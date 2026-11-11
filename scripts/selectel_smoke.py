"""Preemptible GPU lifecycle probe, independent of the ScanBox ML pipeline.

python -m scripts.selectel_smoke --plan
python -m scripts.selectel_smoke --preflight
python -m scripts.selectel_smoke --smoke --report /tmp/selectel-smoke.json
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import time
import uuid

from scripts.orchestrator import Config, SelectelClient, create_ephemeral_keypair


def gpu_probe(cfg, key, host, client, server_id):
    import paramiko
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    deadline = time.monotonic() + 300
    try:
        while True:
            client.check_preempted(client.get_server(server_id))
            try:
                ssh.connect(host, username=cfg.ssh_user, pkey=key, timeout=15,
                            auth_timeout=15, banner_timeout=15,
                            allow_agent=False, look_for_keys=False)
                break
            except (paramiko.SSHException, OSError):
                if time.monotonic() >= deadline:
                    raise TimeoutError("SSH не доступен за 300с; проверьте сеть, образ и security group")
                time.sleep(5)
        _, stdout, stderr = ssh.exec_command(
            "nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader", timeout=30)
        output = stdout.read(8192).decode(errors="replace").strip()
        error = stderr.read(8192).decode(errors="replace").strip()
        code = stdout.channel.recv_exit_status()
        if code != 0 or not output:
            raise RuntimeError(f"nvidia-smi не прошёл: exit={code}; {error[:500]}")
        return output
    finally:
        ssh.close()


def validate_config(cfg, require_credentials=True, require_resources=True):
    required = {
        "SELECTEL_REGION": cfg.region, "SELECTEL_FLAVOR": cfg.flavor,
        "SELECTEL_IMAGE": cfg.image, "SELECTEL_NETWORK_ID": cfg.network_id,
        "SELECTEL_AVAILABILITY_ZONE": cfg.availability_zone,
        "SELECTEL_SECURITY_GROUP": cfg.security_group,
    }
    if not require_resources:
        required = {"OS_REGION_NAME или SELECTEL_REGION": cfg.region}
    if require_credentials:
        cfg.auth_body()
    missing = [key for key, value in required.items() if not value]
    if missing:
        raise ValueError("Задайте переменные: " + ", ".join(missing))


def preflight(cfg, client):
    """Only issue token and perform GETs; no resource config is needed."""
    validate_config(cfg, require_resources=False)
    servers = client._request("GET", "/servers", params={"limit": 100}).get("servers", [])
    flavors = client._request("GET", "/flavors/detail", params={"limit": 100}).get("flavors", [])
    result = {"auth": "ok", "auth_source": cfg.auth_source, "project_id": client.project_id,
              "region": cfg.region, "resources_created": False,
              "servers_on_first_page": len(servers),
              "flavors": [{key: item.get(key) for key in ("id", "name", "vcpus", "ram")}
                          for item in flavors], "page_limit": 100}
    # Query Glance only when an image was explicitly selected. A compute-only
    # viewer can check auth/Nova without needing image-service permissions.
    if cfg.image:
        image = client.find_image(cfg.image)
        result["image"] = {key: image.get(key) for key in ("id", "name", "status")}
    return result


def smoke(cfg, report_path: Path, client=None, probe=gpu_probe):
    """Persist resource IDs before waiting; always attempt both cleanup steps."""
    cfg.preemptible = True
    client = client or SelectelClient(cfg)
    name = "scanbox-smoke-" + uuid.uuid4().hex[:16]
    report = {"name": name, "status": "running", "preemptible": True,
              "server_id": None, "server_deleted": False, "cleanup_errors": [],
              "creation_uncertain": False}
    started = time.monotonic()
    key_attempted = False

    def save():
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")

    save()  # Fail before resource creation if the report cannot be written.
    try:
        flavor = client.find_flavor(cfg.flavor)
        image = client.find_image(cfg.image)
        key_attempted = True
        report["key_name"] = name
        save()
        key = create_ephemeral_keypair(client, name)
        report["creation_uncertain"] = True
        save()
        server = client.create_server(name=name, flavor_id=flavor["id"], image_id=image["id"],
                                      key_name=name, user_data=None)
        report["server_id"] = server["id"]
        report["creation_uncertain"] = False
        save()
        host = client.wait_active(server["id"])
        details = client.get_server(server["id"])
        report["volume_ids"] = [v["id"] for v in details.get("os-extended-volumes:volumes_attached", [])]
        tags = client._request("GET", f"/servers/{server['id']}/tags").get("tags", [])
        report["tags"] = tags
        save()
        if "preemptible" not in tags:
            raise RuntimeError("API не подтвердил тег preemptible — тест не пройден")
        report["gpu"] = probe(cfg, key, host, client, server["id"])
        report["status"] = "passed"
    except Exception as exc:
        report["status"] = "failed"
        report["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if report["server_id"]:
            try:
                client.delete_server(report["server_id"])
                client.wait_deleted(report["server_id"])
                report["server_deleted"] = True
                if cfg.boot_volume_gb and not report.get("volume_ids"):
                    raise RuntimeError("ID сетевого диска не получен; проверьте Cinder вручную")
                client.wait_volumes_deleted(report.get("volume_ids", []))
                report["volumes_deleted"] = True
            except Exception as exc:
                report["cleanup_errors"].append(f"server: {type(exc).__name__}: {exc}")
        if key_attempted:
            try:
                # Unlike the legacy convenience wrapper, fail visibly on errors.
                client._request("DELETE", f"/os-keypairs/{name}")
                report["key_deleted"] = True
            except Exception as exc:
                if getattr(exc, "status_code", None) == 404:
                    report["key_deleted"] = True
                else:
                    report["cleanup_errors"].append(f"keypair: {type(exc).__name__}: {exc}")
        if report["cleanup_errors"] or report["creation_uncertain"]:
            report["status"] = "failed"
        report["elapsed_seconds"] = round(time.monotonic() - started, 2)
        save()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--plan", action="store_true", help="JSON запроса без сети и создания VM")
    mode.add_argument("--preflight", action="store_true", help="только auth и чтение серверов/флейворов")
    mode.add_argument("--smoke", action="store_true", help="создать платную VM, проверить GPU и удалить")
    parser.add_argument("--report", type=Path, default=Path("/tmp/selectel-smoke.json"))
    args = parser.parse_args()
    cfg = Config.from_env()
    cfg.preemptible = True
    client = SelectelClient(cfg)
    if args.plan:
        body = client.server_payload("scanbox-smoke-PLAN", cfg.flavor or "FLAVOR_ID",
                                     cfg.image or "IMAGE_ID", "EPHEMERAL_KEY_NAME", None)
        print(json.dumps({"method": "POST", "path": "/servers",
                          "headers": {"OpenStack-API-Version": "compute 2.72"},
                          "body": body}, ensure_ascii=False, indent=2))
        return 0
    try:
        if args.preflight:
            print(json.dumps(preflight(cfg, client), ensure_ascii=False, indent=2))
            return 0
        validate_config(cfg)
        result = smoke(cfg, args.report, client)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["status"] == "passed" else 1
    except Exception as exc:
        parser.exit(1, f"{type(exc).__name__}: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
