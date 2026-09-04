#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ScanBox AI — эфемерный GPU-оркестратор для Selectel.

Пайплайн одной генерации 3D-модели:

    create VM ──> wait ready ──> upload photo ──> run job ──> download GLB ──> delete VM

Сервер живёт ровно столько, сколько идёт вычисление, и удаляется ВСЕГДА
(даже при ошибке). Это тот же принцип, что у CI-раннеров: compute приезжает
вместе с задачей и умирает после неё.

Быстрый прогон без кредов (локально, ничего не арендует):
    python3 orchestrator.py --mock --photo photo.jpg --out model.glb

Реальный запуск:
    заполни переменные окружения (см. .env.example) и убери --mock

Конфигурация (env):
    SELECTEL_ACCOUNT_ID      аккаунт Selectel (domain)
    SELECTEL_USERNAME        пользователь OpenStack
    SELECTEL_PASSWORD        пароль OpenStack
    SELECTEL_PROJECT_NAME    проект (по умолчанию default)
    SELECTEL_REGION          регион (по умолчанию первый из каталога)
    SELECTEL_FLAVOR          id/имя flavor GPU (по умолчанию ищем GPU-флейворы)
    SELECTEL_IMAGE           id/имя образа (по умолчанию ищем gpu-*/ubuntu-*)
    SELECTEL_NETWORK         имя сети, если нужна конкретная (по умолчанию дефолтная)
    SELECTEL_BOOT_VOLUME_GB  размер загрузочного диска (для GPU обычно нужен, напр. 200)
    SELECTEL_SSH_USER        ssh-пользователь (по умолчанию root)
    SELECTEL_GPU_RATE        тариф GPU, ₽/час — только для сметы (по умолчанию 350)
"""

from __future__ import annotations

import argparse
import base64
import logging
import os
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

IDENTITY_URL = "https://cloud.api.selcloud.ru/identity/v3"
# fallback; реальный URL берём из service catalog при авторизации
DEFAULT_COMPUTE_URL = "https://api.selcloud.ru/compute/v2.1"

EMOJI = {
    "provisioning": "🚀",
    "ready": "🟢",
    "uploading": "📤",
    "generating": "⚙️",
    "done": "✅",
    "deleting": "🧹",
    "deleted": "🏁",
    "failed": "❌",
}

log = logging.getLogger("scanbox")
GENERATE_SCRIPT = Path(__file__).with_name("generate.py")


def status(stage: str, msg: str) -> None:
    """Одна заметная строка статуса — видно на стенде и в логе."""
    print(f"{EMOJI.get(stage, '·')} [{stage}] {msg}", flush=True)


@dataclass
class Config:
    account_id: str
    username: str
    password: str
    project_name: str = "default"
    region: str = ""
    flavor: str = ""
    image: str = ""
    network: str = ""
    boot_volume_gb: int = 0
    ssh_user: str = "root"
    gpu_rate: float = 350.0
    mock: bool = False
    keep_on_error: bool = False

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            account_id=os.environ.get("SELECTEL_ACCOUNT_ID", ""),
            username=os.environ.get("SELECTEL_USERNAME", ""),
            password=os.environ.get("SELECTEL_PASSWORD", ""),
            project_name=os.environ.get("SELECTEL_PROJECT_NAME", "default"),
            region=os.environ.get("SELECTEL_REGION", ""),
            flavor=os.environ.get("SELECTEL_FLAVOR", ""),
            image=os.environ.get("SELECTEL_IMAGE", ""),
            network=os.environ.get("SELECTEL_NETWORK", ""),
            boot_volume_gb=int(os.environ.get("SELECTEL_BOOT_VOLUME_GB", "0")),
            ssh_user=os.environ.get("SELECTEL_SSH_USER", "root"),
            gpu_rate=float(os.environ.get("SELECTEL_GPU_RATE", "350")),
        )


class SelectelClient:
    """Тонкий клиент OpenStack API Selectel (Nova compute)."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.token: str | None = None
        self.project_id: str | None = None
        self.compute_url: str | None = None

    # --- auth -----------------------------------------------------------

    def _auth(self) -> None:
        import requests  # ленивый импорт: mock-режим работает без зависимостей

        body = {
            "auth": {
                "identity": {
                    "methods": ["password"],
                    "password": {
                        "user": {
                            "name": self.cfg.username,
                            "domain": {"name": self.cfg.account_id},
                            "password": self.cfg.password,
                        }
                    },
                },
                "scope": {
                    "project": {
                        "name": self.cfg.project_name,
                        "domain": {"name": self.cfg.account_id},
                    }
                },
            }
        }
        r = requests.post(f"{IDENTITY_URL}/auth/tokens", json=body, timeout=30)
        if r.status_code >= 400:
            raise RuntimeError(f"auth failed ({r.status_code}): {r.text[:300]}")
        self.token = r.headers["X-Subject-Token"]  # токен приходит в заголовке
        data = r.json()
        self.project_id = data["token"]["project"]["id"]
        self.compute_url = self._compute_endpoint(data["token"].get("catalog", []))
        log.info("token ok, project=%s, compute=%s", self.project_id, self.compute_url)

    def _compute_endpoint(self, catalog: list[dict]) -> str:
        candidates: list[str] = []
        for entry in catalog:
            if entry.get("type") != "compute":
                continue
            for ep in entry.get("endpoints", []):
                if self.cfg.region and ep.get("region") != self.cfg.region:
                    continue
                if ep.get("url"):
                    candidates.append(ep["url"])
        if not candidates:
            return DEFAULT_COMPUTE_URL
        url = candidates[0]
        # у некоторых провайдеров в URL есть плейсхолдер проекта
        return url.replace("{project_id}", self.project_id).replace("%(tenant_id)s", self.project_id)

    def _request(self, method: str, path: str, **kw) -> dict:
        import requests

        if not self.token:
            self._auth()
        url = path if path.startswith("http") else f"{self.compute_url}{path}"
        headers = {"X-Auth-Token": self.token, "Content-Type": "application/json"}
        r = requests.request(method, url, headers=headers, timeout=60, **kw)
        if r.status_code == 401:  # токен протух — один ретрай
            self._auth()
            headers["X-Auth-Token"] = self.token
            r = requests.request(method, url, headers=headers, timeout=60, **kw)
        if r.status_code >= 400:
            raise RuntimeError(f"{method} {path} -> {r.status_code}: {r.text[:300]}")
        return r.json() if r.content else {}

    # --- discovery ------------------------------------------------------

    def find_flavor(self, wanted: str = "") -> dict:
        """Выбрать GPU-флейвор: явный (id/имя) или первый GPU по объёму RAM."""
        data = self._request("GET", "/flavors/detail?limit=1000")
        flavors = data.get("flavors", [])
        if wanted:
            for f in flavors:
                if f["id"] == wanted or f["name"] == wanted:
                    return f
            gpu_names = [f["name"] for f in flavors if "gpu" in f["name"].lower()][:10]
            raise RuntimeError(f"flavor '{wanted}' не найден. GPU-флейворы: {gpu_names}")
        gpu = [f for f in flavors if "gpu" in f["name"].lower()]
        if not gpu:
            raise RuntimeError("GPU-флейворы не найдены — укажи SELECTEL_FLAVOR")
        return sorted(gpu, key=lambda f: f.get("ram", 0), reverse=True)[0]

    def find_image(self, wanted: str = "") -> dict:
        """Образ ОС: явный, либо GPU-образ, либо ubuntu."""
        images = self._request("GET", "/images?limit=1000").get("images", [])
        if wanted:
            for im in images:
                if im["id"] == wanted or wanted.lower() in im["name"].lower():
                    return im
            raise RuntimeError(f"образ '{wanted}' не найден — укажи SELECTEL_IMAGE")
        active = [im for im in images if im.get("status") == "ACTIVE"]
        for im in active:
            if "gpu" in im["name"].lower():
                return im
        for im in active:
            if "ubuntu" in im["name"].lower():
                return im
        raise RuntimeError("подходящий образ не найден — укажи SELECTEL_IMAGE")

    # --- keypair --------------------------------------------------------

    def create_keypair(self, name: str, public_key: str) -> None:
        self._request("POST", "/os-keypairs", json={"keypair": {"name": name, "public_key": public_key}})

    def delete_keypair(self, name: str) -> None:
        try:
            self._request("DELETE", f"/os-keypairs/{name}")
        except Exception as exc:  # 404 на повторе — не критично
            log.warning("не удалось удалить keypair %s: %s", name, exc)

    # --- server ---------------------------------------------------------

    def create_server(self, name: str, flavor_id: str, image_id: str, key_name: str, user_data: str | None) -> dict:
        server = {
            "name": name,
            "flavorRef": flavor_id,
            "imageRef": image_id,
            "key_name": key_name,
            "networks": [{"name": self.cfg.network}] if self.cfg.network else [],
            "metadata": {"scanbox-job": name},
        }
        if self.cfg.boot_volume_gb:
            server.pop("imageRef")
            # GPU-серверы Selectel обычно грузятся с диска (Cinder);
            # delete_on_termination=True — диск умирает вместе с сервером.
            server["block_device_mapping_v2"] = [{
                "uuid": image_id,
                "source_type": "image",
                "destination_type": "volume",
                "boot_index": 0,
                "delete_on_termination": True,
                "volume_size": self.cfg.boot_volume_gb,
            }]
        if user_data:
            server["user_data"] = base64.b64encode(user_data.encode()).decode()
        return self._request("POST", "/servers", json={"server": server})["server"]

    def wait_active(self, server_id: str, timeout: int = 600, poll: int = 10) -> str:
        deadline = time.time() + timeout
        while time.time() < deadline:
            server = self._request("GET", f"/servers/{server_id}")["server"]
            status_name = server.get("status")
            if status_name == "ACTIVE":
                return self.server_ip(server)
            if status_name == "ERROR":
                raise RuntimeError(f"сервер {server_id} упал в ERROR")
            time.sleep(poll)
        raise TimeoutError(f"сервер {server_id} не стал ACTIVE за {timeout}с")

    @staticmethod
    def server_ip(server: dict) -> str:
        for net in (server.get("addresses") or {}).values():
            for addr in net:
                if addr.get("version") == 4:
                    return addr["addr"]
        raise RuntimeError("у сервера нет IPv4-адреса")

    def delete_server(self, server_id: str) -> None:
        try:
            self._request("DELETE", f"/servers/{server_id}")
        except Exception as exc:  # 404 на повторе — уже удалён
            log.warning("не удалось удалить сервер %s: %s", server_id, exc)


class MockClient:
    """Тот же интерфейс, что SelectelClient, но без единого запроса наружу."""

    def __init__(self, cfg: Config):
        self.cfg = cfg

    def find_flavor(self, wanted: str = "") -> dict:
        return {"id": "fl-mock-gpu", "name": wanted or "gpu-rtx4090-mock", "vcpus": 8, "ram": 32768}

    def find_image(self, wanted: str = "") -> dict:
        return {"id": "img-mock-ubuntu", "name": wanted or "gpu-ubuntu-22.04-mock"}

    def create_keypair(self, name: str, public_key: str) -> None:
        log.info("mock: keypair %s создан", name)

    def delete_keypair(self, name: str) -> None:
        log.info("mock: keypair %s удалён", name)

    def create_server(self, **kw) -> dict:
        time.sleep(2)
        return {"id": f"mock-{uuid.uuid4().hex[:8]}", "name": kw["name"]}

    def wait_active(self, server_id: str, timeout: int = 600, poll: int = 10) -> str:
        time.sleep(2)
        return "203.0.113.10"

    def delete_server(self, server_id: str) -> None:
        time.sleep(1)


# --- SSH-обвязка ---------------------------------------------------------

def create_ephemeral_keypair(client, name: str):
    """Генерим SSH-ключ на джобу и сразу забываем: приватный живёт только в памяти."""
    import paramiko

    key = paramiko.RSAKey.generate(2048)
    client.create_keypair(name, f"{key.get_name()} {key.get_base64()} scanbox-ephemeral")
    return key


def ssh_run_fetch(cfg: Config, key, host: str, photo: Path, out: Path) -> None:
    """Залить фото + джобу, выполнить на GPU, забрать GLB."""
    import paramiko

    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    deadline = time.time() + 300
    while True:
        try:
            ssh.connect(host, username=cfg.ssh_user, pkey=key, timeout=15)
            break
        except Exception as exc:  # SSH поднимается чуть позже статуса ACTIVE
            if time.time() > deadline:
                raise TimeoutError(f"SSH к {host} не поднялся за 5 минут: {exc}")
            time.sleep(10)
    try:
        with ssh.open_sftp() as sftp:
            sftp.put(str(photo), "/tmp/photo.jpg")
            sftp.put(str(GENERATE_SCRIPT), "/tmp/generate.py")
        cmd = "python3 /tmp/generate.py --input /tmp/photo.jpg --output /tmp/model.glb"
        log.info("remote: %s", cmd)
        _, stdout, stderr = ssh.exec_command(cmd, timeout=1800)
        exit_code = stdout.channel.recv_exit_status()
        log.debug("stdout:\n%s", stdout.read().decode())
        err = stderr.read().decode()
        if exit_code != 0:
            raise RuntimeError(f"генерация упала (exit {exit_code}): {err[:500]}")
        with ssh.open_sftp() as sftp:
            sftp.get("/tmp/model.glb", str(out))
    finally:
        ssh.close()


def user_data() -> str | None:
    """cloud-init для свежего сервера (пока нет golden-image)."""
    path = Path(__file__).with_name("cloud-init.yaml")
    return path.read_text() if path.exists() else None


# --- пайплайн ------------------------------------------------------------

def run_job(cfg: Config, photo: Path, out: Path, job_id: str) -> None:
    started = time.monotonic()
    client = MockClient(cfg) if cfg.mock else SelectelClient(cfg)
    server_id: str | None = None
    key_name: str | None = None
    try:
        flavor = client.find_flavor(cfg.flavor)
        image = client.find_image(cfg.image)
        log.info("flavor=%s, image=%s", flavor["name"], image["name"])

        key = None
        if not cfg.mock:  # mock-режим работает без paramiko и без ключей
            key_name = f"scanbox-{job_id}"
            key = create_ephemeral_keypair(client, key_name)

        status("provisioning", f"создаём VM с GPU ({flavor['name']}) ...")
        server = client.create_server(
            name=f"scanbox-{job_id}",
            flavor_id=flavor["id"],
            image_id=image["id"],
            key_name=key_name,
            user_data=None if cfg.mock else user_data(),
        )
        server_id = server["id"]
        ip = client.wait_active(server_id)
        status("ready", f"сервер {server_id} активен, ip={ip}")

        if cfg.mock:
            status("generating", "mock: генерируем модель локально ...")
            subprocess.run(
                [sys.executable, str(GENERATE_SCRIPT), "--input", str(photo), "--output", str(out), "--engine", "mock"],
                check=True,
            )
        else:
            status("uploading", f"шлём фото и джобу на {ip} ...")
            ssh_run_fetch(cfg, key, ip, photo, out)
            status("generating", "генерация завершена на GPU")

        status("done", f"модель: {out}")
    except Exception as exc:
        status("failed", str(exc))
        if cfg.keep_on_error:
            log.warning(
                "--keep-on-error: сервер НЕ удалён (id=%s) — сними флаг, чтобы гарантировать удаление", server_id
            )
            return
        raise
    finally:
        if server_id and not cfg.keep_on_error:
            status("deleting", "удаляем сервер ...")
            client.delete_server(server_id)
            status("deleted", "сервер удалён")
        if key_name:
            client.delete_keypair(key_name)
        minutes = (time.monotonic() - started) / 60
        log.info(
            "итого: %.1f мин под нагрузкой, смета ≈ %.2f ₽ (тариф %.0f ₽/ч)",
            minutes, minutes / 60 * cfg.gpu_rate, cfg.gpu_rate,
        )


def main() -> int:
    ap = argparse.ArgumentParser(
        description="ScanBox AI: эфемерный GPU для одной генерации 3D-модели (Selectel)."
    )
    ap.add_argument("--photo", required=True, help="входное фото")
    ap.add_argument("--out", default="model.glb", help="куда сохранить GLB")
    ap.add_argument("--job-id", default=uuid.uuid4().hex[:8], help="имя джобы/сервера")
    ap.add_argument("--mock", action="store_true", help="не трогать Selectel — прогнать пайплайн локально")
    ap.add_argument("--keep-on-error", action="store_true",
                    help="НЕ удалять сервер при ошибке (для отладки; жжёт деньги!)")
    ap.add_argument("--flavor", default="", help="id/имя flavor (или SELECTEL_FLAVOR)")
    ap.add_argument("--image", default="", help="id/имя образа (или SELECTEL_IMAGE)")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s" if args.verbose else "%(message)s",
    )

    photo = Path(args.photo)
    if not photo.exists():
        log.error("фото не найдено: %s", photo)
        return 2

    cfg = Config.from_env()
    cfg.mock = args.mock
    cfg.keep_on_error = args.keep_on_error
    if args.flavor:
        cfg.flavor = args.flavor
    if args.image:
        cfg.image = args.image

    if not cfg.mock and not (cfg.account_id and cfg.username and cfg.password):
        log.error(
            "Нет креденшелов Selectel. Заполни SELECTEL_ACCOUNT_ID/USERNAME/PASSWORD "
            "(шаблон в scripts/.env.example) или запусти с --mock"
        )
        return 2

    run_job(cfg, photo, Path(args.out), args.job_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())