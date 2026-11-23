import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from scripts.orchestrator import Config, SelectelAPIError, run_job
from scripts.selectel_network import NeutronClient
from scripts.selectel_smoke import preflight, smoke, validate_config


def cfg():
    return Config("account", "user", "test-password", region="ru-6", flavor="13960",
                  image="image", network_id="private", security_group="default",
                  availability_zone="ru-6a", floating_network_id="external")


class NeutronTests(unittest.TestCase):
    def client(self):
        identity = Mock(token="token", project_id="project")
        identity._endpoint.return_value = "https://neutron.example/v2.0"
        return NeutronClient(identity)

    @patch("requests.request")
    def test_network_api_has_its_own_base_and_no_nova_header(self, request):
        request.return_value = SimpleNamespace(status_code=200, content=b"{}", json=lambda: {})
        client = self.client()
        client.request("GET", "/ports")
        self.assertEqual(request.call_args.args[:2], ("GET", "https://neutron.example/v2.0/ports"))
        self.assertNotIn("OpenStack-API-Version", request.call_args.kwargs["headers"])

    @patch("requests.request")
    def test_ambiguous_post_is_not_retried(self, request):
        request.side_effect = TimeoutError("connection lost")
        with self.assertRaises(TimeoutError):
            self.client().create_floating_ip("external", "port", "192.168.50.5", "test")
        request.assert_called_once()

    def topology(self):
        client = self.client()
        client.request = Mock(side_effect=[
            {"network": {"port_security_enabled": True, "router:external": False}},
            {"network": {"router:external": True}},
            {"ports": [{"device_id": "router", "device_owner": "network:router_interface"}]},
            {"routers": [{"id": "router", "external_gateway_info": {"network_id": "external"}}]},
        ])
        return client

    def test_valid_topology_only_uses_gets(self):
        client = self.topology()
        result = client.validate_topology("private", "external")
        self.assertEqual(result["router_id"], "router")
        self.assertFalse(result["resources_created"])
        self.assertTrue(all(call.args[0] == "GET" for call in client.request.call_args_list))

    def test_same_private_and_external_network_rejected_without_api(self):
        client = self.client()
        client.request = Mock()
        with self.assertRaises(ValueError):
            client.validate_topology("external", "external")
        client.request.assert_not_called()

    def test_missing_gateway_rejected_before_vm_creation(self):
        client = self.topology()
        client.request.side_effect = [
            {"network": {"port_security_enabled": True}},
            {"network": {"router:external": True}}, {"ports": []}, {"routers": []}]
        with self.assertRaisesRegex(ValueError, "роутер"):
            client.validate_topology("private", "external")

    def test_null_gateway_is_handled(self):
        client = self.topology()
        client.request.side_effect = [
            {"network": {"port_security_enabled": True}},
            {"network": {"router:external": True}},
            {"ports": [{"device_id": "router", "device_owner": "network:router_interface"}]},
            {"routers": [{"id": "router", "external_gateway_info": None}]}]
        with self.assertRaises(ValueError):
            client.validate_topology("private", "external")

    def test_port_security_is_required(self):
        client = self.client()
        client.request = Mock(side_effect=[
            {"network": {"port_security_enabled": False}},
            {"network": {"router:external": True}}])
        with self.assertRaisesRegex(ValueError, "port_security"):
            client.validate_topology("private", "external")

    def test_selects_only_vm_ipv4_on_requested_network(self):
        client = self.client()
        client.request = Mock(return_value={"ports": [
            {"id": "wrong", "device_id": "other-vm", "network_id": "private",
             "fixed_ips": [{"ip_address": "192.168.50.6"}]},
            {"id": "port", "device_id": "vm", "network_id": "private",
             "fixed_ips": [{"ip_address": "2001:db8::1"}, {"ip_address": "192.168.50.5"}]}]})
        self.assertEqual(client.server_port("vm", "private"), ("port", "192.168.50.5"))

    def test_ambiguous_vm_ports_are_rejected(self):
        client = self.client()
        client.request = Mock(return_value={"ports": [
            {"id": str(i), "device_id": "vm", "network_id": "private",
             "fixed_ips": [{"ip_address": f"192.168.50.{i + 5}"}]} for i in range(2)]})
        with self.assertRaises(ValueError):
            client.server_port("vm", "private")

    def test_floating_ip_is_bound_in_create_request(self):
        client = self.client()
        client.request = Mock(return_value={"floatingip": {"id": "fip"}})
        client.create_floating_ip("external", "port", "192.168.50.5", "smoke-unique")
        body = client.request.call_args.kwargs["json"]["floatingip"]
        self.assertEqual(body, {"floating_network_id": "external", "port_id": "port",
                               "fixed_ip_address": "192.168.50.5", "description": "smoke-unique"})

    def test_deleted_ip_is_idempotent_and_other_errors_propagate(self):
        client = self.client()
        client.request = Mock(side_effect=SelectelAPIError(404, "gone"))
        client.delete_floating_ip("fip")
        client.wait_deleted("fip", timeout=1, poll=0)
        client.request.side_effect = SelectelAPIError(403, "denied")
        with self.assertRaises(SelectelAPIError):
            client.delete_floating_ip("fip")


class FloatingSmokeTests(unittest.TestCase):
    def setup_clients(self):
        nova = Mock()
        nova.find_flavor.return_value = {"id": "13960", "name": "gpu"}
        nova.find_image.return_value = {"id": "image", "name": "image"}
        nova.create_server.return_value = {"id": "vm"}
        nova.wait_active.return_value = "192.168.50.5"
        nova.get_server.return_value = {"os-extended-volumes:volumes_attached": []}
        nova._request.return_value = {"tags": ["preemptible"]}
        network = Mock()
        network.validate_topology.return_value = {"resources_created": False}
        network.server_port.return_value = ("port", "192.168.50.5")
        network.create_floating_ip.return_value = {"id": "fip", "floating_ip_address": "203.0.113.5"}
        return nova, network

    def execute(self, nova, network, probe):
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "report.json"
            with patch("scripts.selectel_smoke.NeutronClient", return_value=network), \
                    patch("scripts.selectel_smoke.create_ephemeral_keypair", return_value="key"):
                result = smoke(cfg(), report, nova, probe)
            self.assertEqual(json.loads(report.read_text()), result)
            return result

    def test_smoke_uses_public_ip_and_deletes_only_its_allocated_ip(self):
        nova, network = self.setup_clients()
        probe = Mock(return_value="GPU")
        report = self.execute(nova, network, probe)
        self.assertEqual(report["status"], "passed")
        self.assertEqual(probe.call_args.args[2], "203.0.113.5")
        self.assertTrue(report["floating_ip_deleted"])
        network.delete_floating_ip.assert_called_once_with("fip")
        network.wait_deleted.assert_called_once_with("fip")
        nova.delete_server.assert_called_once_with("vm")

    def test_topology_failure_happens_before_key_or_vm_creation(self):
        nova, network = self.setup_clients()
        network.validate_topology.side_effect = ValueError("no router")
        report = self.execute(nova, network, Mock())
        self.assertEqual(report["status"], "failed")
        nova.create_server.assert_not_called()
        network.create_floating_ip.assert_not_called()

    def test_lost_allocation_response_requires_manual_reconciliation(self):
        nova, network = self.setup_clients()
        network.create_floating_ip.side_effect = TimeoutError("timeout")
        report = self.execute(nova, network, Mock())
        self.assertTrue(report["floating_ip_creation_uncertain"])
        self.assertEqual(report["status"], "failed")
        network.create_floating_ip.assert_called_once()
        network.delete_floating_ip.assert_not_called()
        nova.delete_server.assert_called_once_with("vm")

    def test_ip_cleanup_failure_does_not_skip_vm_cleanup(self):
        nova, network = self.setup_clients()
        network.delete_floating_ip.side_effect = SelectelAPIError(403, "denied")
        report = self.execute(nova, network, Mock(return_value="GPU"))
        self.assertEqual(report["status"], "failed")
        self.assertTrue(report["cleanup_errors"])
        self.assertTrue(report["server_deleted"])
        self.assertTrue(report["key_deleted"])

    def test_preflight_checks_topology_without_allocating(self):
        nova, network = self.setup_clients()
        nova._request.side_effect = [{"servers": []}, {"flavors": []}]
        with patch("scripts.selectel_smoke.NeutronClient", return_value=network):
            result = preflight(cfg(), nova)
        self.assertFalse(result["resources_created"])
        network.validate_topology.assert_called_once_with("private", "external")
        network.create_floating_ip.assert_not_called()

    def test_same_network_settings_rejected(self):
        settings = cfg()
        settings.network_id = settings.floating_network_id
        with self.assertRaises(ValueError):
            validate_config(settings)


class FloatingOrchestratorTests(unittest.TestCase):
    setup_clients = FloatingSmokeTests.setup_clients

    @patch("scripts.orchestrator.ssh_run_fetch")
    @patch("scripts.orchestrator.create_ephemeral_keypair", return_value="key")
    @patch("scripts.orchestrator.SelectelClient")
    @patch("scripts.selectel_network.NeutronClient")
    def test_generation_uses_public_ip_and_cleans_it(self, net_factory, nova_factory, key, generate):
        nova, network = self.setup_clients()
        nova_factory.return_value = nova
        net_factory.return_value = network
        run_job(cfg(), Path("photo.png"), Path("model.glb"), "unique")
        self.assertEqual(generate.call_args.args[2], "203.0.113.5")
        network.delete_floating_ip.assert_called_once_with("fip")
        nova.delete_server.assert_called_once_with("vm")


if __name__ == "__main__":
    unittest.main()
