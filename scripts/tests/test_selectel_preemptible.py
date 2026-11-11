import json
import io
import os
from contextlib import redirect_stdout
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from scripts.orchestrator import (
    Config, SelectelAPIError, SelectelClient, ServerPreempted, run_job,
)
from scripts.selectel_smoke import main, preflight, smoke, validate_config


def config(**kwargs):
    return Config("test-account", "test-user", "test-password", preemptible=True,
                  region="ru-7", network_id="network-uuid", availability_zone="ru-7a",
                  security_group="ssh-runner", flavor="gpu-flavor", image="gpu-image", **kwargs)


class SelectelTests(unittest.TestCase):
    def test_preemptible_payload_and_boot_volume_cleanup(self):
        client = SelectelClient(config(boot_volume_gb=200))
        body = client.server_payload("test", "flavor", "image", "key", None)["server"]
        self.assertEqual(body["tags"], ["preemptible"])
        self.assertEqual(body["networks"], [{"uuid": "network-uuid"}])
        self.assertEqual(body["availability_zone"], "ru-7a")
        self.assertEqual(body["security_groups"], [{"name": "ssh-runner"}])
        self.assertNotIn("imageRef", body)
        disk = body["block_device_mapping_v2"][0]
        self.assertTrue(disk["delete_on_termination"])
        self.assertEqual(disk["uuid"], "image")

    def test_nonpreemptible_remains_supported(self):
        cfg = config()
        cfg.preemptible = False
        body = SelectelClient(cfg).server_payload("test", "f", "i", "k", None)["server"]
        self.assertNotIn("tags", body)

    @patch("requests.request")
    def test_microversion_is_sent_to_nova(self, request):
        request.return_value = SimpleNamespace(status_code=200, content=b"{}", json=lambda: {})
        client = SelectelClient(config())
        client.token = "test-token"
        client.compute_url = "https://compute.example/v2.1"
        client._request("GET", "/servers")
        self.assertEqual(request.call_args.kwargs["headers"]["OpenStack-API-Version"], "compute 2.72")

    def test_catalog_uses_public_region_and_refuses_fallback(self):
        client = SelectelClient(config())
        client.project_id = "project-id"
        catalog = [{"type": "compute", "endpoints": [
            {"region": "ru-7", "interface": "internal", "url": "https://internal.example"},
            {"region": "ru-1", "interface": "public", "url": "https://wrong.example"},
            {"region": "ru-7", "interface": "public", "url": "https://correct.example/{project_id}"},
        ]}]
        self.assertEqual(client._compute_endpoint(catalog), "https://correct.example/project-id")
        with self.assertRaisesRegex(RuntimeError, "endpoint"):
            client._compute_endpoint([])

    def test_provider_preemption_detected_during_provisioning(self):
        for state in ("EXPIRED", "SHELVED_OFFLOADED"):
            with self.subTest(state=state):
                client = SelectelClient(config())
                client.get_server = Mock(return_value={"id": "vm", "status": state})
                with self.assertRaises(ServerPreempted):
                    client.wait_active("vm", timeout=1, poll=0)

    def test_normal_vm_offload_not_mislabeled_as_preemption(self):
        cfg = config()
        cfg.preemptible = False
        SelectelClient(cfg).check_preempted({"status": "SHELVED_OFFLOADED"})

    def test_cleanup_api_failure_is_not_silenced(self):
        client = SelectelClient(config())
        client._request = Mock(side_effect=SelectelAPIError(403, "denied"))
        with self.assertRaises(SelectelAPIError):
            client.delete_server("vm")

    def test_already_deleted_vm_is_ok(self):
        client = SelectelClient(config())
        client._request = Mock(side_effect=SelectelAPIError(404, "gone"))
        client.delete_server("vm")
        client.wait_deleted("vm", timeout=1, poll=0)

    @patch("requests.get")
    def test_cinder_cleanup_is_verified(self, get):
        get.side_effect = [SimpleNamespace(status_code=200), SimpleNamespace(status_code=404)]
        client = SelectelClient(config())
        client.token, client.project_id = "token", "project"
        client.catalog = [{"type": "volumev3", "endpoints": [
            {"region": "ru-7", "interface": "public", "url": "https://volume.example/v3/project"}]}]
        client.wait_volumes_deleted(["disk"], timeout=1, poll=0)
        self.assertEqual(get.call_count, 2)
        self.assertTrue(get.call_args.args[0].endswith("/volumes/disk"))

    @patch("scripts.orchestrator.create_ephemeral_keypair", return_value="key")
    @patch("scripts.orchestrator.SelectelClient")
    def test_generation_pipeline_cleans_up_after_preemption(self, factory, create_key):
        client = factory.return_value
        client.find_flavor.return_value = {"id": "f", "name": "gpu"}
        client.find_image.return_value = {"id": "i", "name": "image"}
        client.create_server.return_value = {"id": "vm"}
        client.wait_active.side_effect = ServerPreempted("expired")
        with self.assertRaises(ServerPreempted):
            run_job(config(), Path("photo"), Path("model"), "job")
        client.delete_server.assert_called_once_with("vm")
        client.wait_deleted.assert_called_once_with("vm")
        client.delete_keypair.assert_called_once_with("scanbox-job")


class RcAuthTests(unittest.TestCase):
    RC = {"OS_AUTH_URL": "https://identity.example/v3/", "OS_AUTH_TYPE": "v3password",
          "OS_USERNAME": "service-user", "OS_PASSWORD": "test-password",
          "OS_USER_DOMAIN_NAME": "account-domain", "OS_PROJECT_ID": "project-id",
          "OS_PROJECT_NAME": "do-not-use-this-name", "OS_REGION_NAME": "ru-7"}

    def test_rc_project_id_takes_priority_over_name(self):
        with patch.dict(os.environ, self.RC, clear=True):
            cfg = Config.from_env()
        body = cfg.auth_body()["auth"]
        self.assertEqual(cfg.auth_source, "openstack")
        self.assertEqual(cfg.region, "ru-7")
        self.assertEqual(body["scope"]["project"], {"id": "project-id"})
        self.assertEqual(body["identity"]["password"]["user"]["domain"], {"name": "account-domain"})

    def test_rc_user_id_does_not_need_domain(self):
        with patch.dict(os.environ, {**self.RC, "OS_USER_ID": "user-id"}, clear=True):
            cfg = Config.from_env()
        user = cfg.auth_body()["auth"]["identity"]["password"]["user"]
        self.assertEqual(user, {"id": "user-id", "password": "test-password"})

    def test_project_name_and_domain_id(self):
        env = {**self.RC, "OS_PROJECT_DOMAIN_ID": "domain-id"}
        del env["OS_PROJECT_ID"]
        with patch.dict(os.environ, env, clear=True):
            cfg = Config.from_env()
        self.assertEqual(cfg.auth_body()["auth"]["scope"]["project"], {
            "name": "do-not-use-this-name", "domain": {"id": "domain-id"}})

    def test_legacy_credentials_are_not_mixed_with_rc_scope(self):
        with patch.dict(os.environ, {**self.RC, "SELECTEL_USERNAME": "legacy-user",
                                    "SELECTEL_PASSWORD": "legacy-password",
                                    "SELECTEL_ACCOUNT_ID": "legacy-domain",
                                    "SELECTEL_AUTH_SOURCE": "selectel"}, clear=True):
            cfg = Config.from_env()
        self.assertEqual(cfg.auth_source, "selectel")
        self.assertEqual(cfg.auth_body()["auth"]["scope"]["project"], {
            "name": "default", "domain": {"name": "legacy-domain"}})

    def test_partial_legacy_env_does_not_override_rc(self):
        with patch.dict(os.environ, {**self.RC, "SELECTEL_USERNAME": "leftover-user",
                                    "SELECTEL_ACCOUNT_ID": "leftover-domain",
                                    "SELECTEL_PASSWORD": ""}, clear=True):
            cfg = Config.from_env()
        self.assertEqual(cfg.auth_source, "openstack")
        user = cfg.auth_body()["auth"]["identity"]["password"]["user"]
        self.assertEqual(user["name"], "service-user")
        self.assertEqual(user["password"], "test-password")

    def test_incomplete_rc_does_not_borrow_legacy_password(self):
        with patch.dict(os.environ, {**self.RC, "OS_PASSWORD": "",
                                    "SELECTEL_PASSWORD": "other-identity-password"}, clear=True):
            cfg = Config.from_env()
        with self.assertRaisesRegex(ValueError, "OS_PASSWORD"):
            cfg.auth_body()

    def test_openstack_can_be_selected_without_exported_rc(self):
        with patch.dict(os.environ, {"SELECTEL_AUTH_SOURCE": "openstack"}, clear=True):
            cfg = Config.from_env()
        with self.assertRaisesRegex(ValueError, "OS_PASSWORD"):
            cfg.auth_body()

    def test_invalid_auth_source_is_rejected(self):
        with patch.dict(os.environ, {"SELECTEL_AUTH_SOURCE": "typo"}, clear=True):
            with self.assertRaisesRegex(ValueError, "SELECTEL_AUTH_SOURCE"):
                Config.from_env()

    def test_env_diagnostics_never_print_credentials_or_make_requests(self):
        output = io.StringIO()
        with patch.dict(os.environ, self.RC, clear=True), \
                patch("sys.argv", ["selectel_smoke", "--check-env"]), \
                patch("requests.post") as post, patch("requests.get") as get, \
                redirect_stdout(output):
            self.assertEqual(main(), 0)
        data = json.loads(output.getvalue())
        self.assertEqual(data["auth_source"], "openstack")
        self.assertTrue(data["variables_present"]["OS_PASSWORD"])
        self.assertNotIn("test-password", output.getvalue())
        self.assertNotIn("service-user", output.getvalue())
        post.assert_not_called()
        get.assert_not_called()

    def test_missing_password_names_variable_without_exposing_other_values(self):
        with patch.dict(os.environ, {**self.RC, "OS_PASSWORD": ""}, clear=True):
            cfg = Config.from_env()
        with self.assertRaisesRegex(ValueError, "OS_PASSWORD") as error:
            cfg.auth_body()
        self.assertNotIn("service-user", str(error.exception))

    def test_unsupported_auth_type_fails_explicitly(self):
        with patch.dict(os.environ, {**self.RC, "OS_AUTH_TYPE": "v3applicationcredential"}, clear=True):
            cfg = Config.from_env()
        with self.assertRaisesRegex(ValueError, "OS_AUTH_TYPE"):
            cfg.auth_body()

    @patch("requests.post")
    def test_token_request_uses_rc_endpoint_and_project_id(self, post):
        post.return_value = SimpleNamespace(status_code=201, headers={"X-Subject-Token": "test-token"},
            json=lambda: {"token": {"project": {"id": "project-id"}, "catalog": [{
                "type": "compute", "endpoints": [{"interface": "public", "region": "ru-7",
                                                    "url": "https://nova.example/v2.1"}]}]}})
        with patch.dict(os.environ, self.RC, clear=True):
            client = SelectelClient(Config.from_env())
        client._auth()
        self.assertEqual(post.call_args.args[0], "https://identity.example/v3/auth/tokens")
        self.assertEqual(post.call_args.kwargs["json"]["auth"]["scope"]["project"], {"id": "project-id"})

    def test_preflight_without_resource_settings_only_uses_get(self):
        with patch.dict(os.environ, self.RC, clear=True):
            cfg = Config.from_env()
        client = SelectelClient(cfg)
        client._request = Mock(side_effect=[{"servers": []}, {"flavors": []}])
        client.find_image = Mock()
        result = preflight(cfg, client)
        self.assertFalse(result["resources_created"])
        self.assertEqual(result["servers_on_first_page"], 0)
        self.assertEqual(client._request.call_count, 2)
        self.assertTrue(all(call.args[0] == "GET" for call in client._request.call_args_list))
        client.find_image.assert_not_called()

    @patch("requests.get")
    def test_image_selection_uses_glance_without_nova_microversion(self, get):
        get.return_value = SimpleNamespace(status_code=200, json=lambda: {
            "images": [{"id": "id", "name": "gpu-image", "status": "active"}]})
        client = SelectelClient(config())
        client.token, client.project_id = "test-token", "project-id"
        client.catalog = [{"type": "image", "endpoints": [{"interface": "public", "region": "ru-7",
                                                            "url": "https://glance.example"}]}]
        self.assertEqual(client.find_image("gpu-image")["id"], "id")
        self.assertEqual(get.call_args.args[0], "https://glance.example/v2/images")
        self.assertNotIn("OpenStack-API-Version", get.call_args.kwargs["headers"])


class SmokeTests(unittest.TestCase):
    def fake_client(self, tags=None):
        client = Mock()
        client.find_flavor.return_value = {"id": "f"}
        client.find_image.return_value = {"id": "i"}
        client.create_server.return_value = {"id": "vm"}
        client.wait_active.return_value = "192.0.2.1"
        client.get_server.return_value = {"os-extended-volumes:volumes_attached": [{"id": "disk"}]}
        client._request.return_value = {"tags": ["preemptible"] if tags is None else tags}
        return client

    def execute(self, client, probe):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            with patch("scripts.selectel_smoke.create_ephemeral_keypair", return_value="key"):
                result = smoke(config(boot_volume_gb=200), path, client, probe)
            self.assertEqual(json.loads(path.read_text()), result)
            return result

    def test_smoke_lifecycle_passes_and_checks_disks(self):
        client = self.fake_client()
        report = self.execute(client, Mock(return_value="GPU, 49140 MiB, 580.0"))
        self.assertEqual(report["status"], "passed")
        self.assertTrue(report["server_deleted"])
        self.assertTrue(report["volumes_deleted"])
        client.wait_volumes_deleted.assert_called_once_with(["disk"])

    def test_missing_preemptible_tag_fails_but_deletes_vm(self):
        client = self.fake_client(tags=[])
        probe = Mock()
        report = self.execute(client, probe)
        self.assertEqual(report["status"], "failed")
        self.assertTrue(report["server_deleted"])
        probe.assert_not_called()

    def test_gpu_failure_still_deletes_vm_and_disks(self):
        client = self.fake_client()
        report = self.execute(client, Mock(side_effect=RuntimeError("no driver")))
        self.assertEqual(report["status"], "failed")
        self.assertTrue(report["server_deleted"])
        self.assertTrue(report["key_deleted"])

    def test_cleanup_failure_is_failed_test(self):
        client = self.fake_client()
        client.delete_server.side_effect = RuntimeError("unavailable")
        report = self.execute(client, Mock(return_value="GPU"))
        self.assertEqual(report["status"], "failed")
        self.assertTrue(report["key_deleted"])
        self.assertTrue(report["cleanup_errors"])

    def test_ambiguous_create_is_not_automatically_retried(self):
        client = self.fake_client()
        client.create_server.side_effect = TimeoutError("timeout")
        report = self.execute(client, Mock())
        self.assertTrue(report["creation_uncertain"])
        self.assertEqual(report["status"], "failed")
        client.create_server.assert_called_once()
        client.delete_server.assert_not_called()

    def test_credentials_are_required_before_live_test(self):
        with self.assertRaisesRegex(ValueError, "SELECTEL_PASSWORD"):
            validate_config(Config("", "", ""))


if __name__ == "__main__":
    unittest.main()
