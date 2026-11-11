#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ScanBox AI — эфемерный GPU-оркестратор для Selectel.

Пайплайн одной генерации 3D-модели:

    create VM ──> wait ready ──> upload photo ──> run job ──> download GLB ──> delete VM

После задачи (включая ошибку) выполняется удаление VM с проверкой результата.
Если API удаления недоступен или процесс runner оборван, нужна внешняя очистка.
Флаг --keep-on-error оставляет VM для диагностики.

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
    SELECTEL_PREEMPTIBLE     1 — создать прерываемую VM (тег preemptible, API 2.72)
    SELECTEL_AVAILABILITY_ZONE сегмент пула, например ru-7a
    SELECTEL_NETWORK_ID      UUID сети (предпочтительно вместо SELECTEL_NETWORK)
    SELECTEL_SECURITY_GROUP группа безопасности с доступом SSH от runner
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


class SelectelAPIError(RuntimeError):
    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        super().__init__(message)


class ServerPreempted(RuntimeError):
    """Прерываемая VM остановлена; результат нельзя считать готовым."""


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
    preemptible: bool = False
    availability_zone: str = ""
    network_id: str = ""
    security_group: str = ""
    auth_url: str = IDENTITY_URL
    auth_source: str = "selectel"
    user_id: str = ""
    user_domain_id: str = ""
    user_domain_name: str = ""
    project_id: str = ""
    project_domain_id: str = ""
    project_domain_name: str = ""
    auth_type: str = "v3password"

    @classmethod
    def from_env(cls) -> "Config":
        cfg = cls(
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
            preemptible=os.environ.get("SELECTEL_PREEMPTIBLE", "0").lower() in ("1", "true", "yes"),
            availability_zone=os.environ.get("SELECTEL_AVAILABILITY_ZONE", ""),
            network_id=os.environ.get("SELECTEL_NETWORK_ID", ""),
            security_group=os.environ.get("SELECTEL_SECURITY_GROUP", ""),
        )
        # Prefer the RC identity when present. A leftover SELECTEL_USERNAME must
        # not switch away from RC or cause a cross-identity password fallback.
        source = os.environ.get("SELECTEL_AUTH_SOURCE", "auto").strip().lower()
        if source not in {"auto", "openstack", "selectel"}:
            raise ValueError("SELECTEL_AUTH_SOURCE: допустимы auto, openstack, selectel")
        has_rc = any(os.environ.get(key) for key in ("OS_AUTH_URL", "OS_USERNAME", "OS_USER_ID"))
        if source == "openstack" or (source == "auto" and has_rc):
            cfg.auth_source = "openstack"
            cfg.auth_url = os.environ.get("OS_AUTH_URL", IDENTITY_URL).rstrip("/")
            cfg.username = os.environ.get("OS_USERNAME", "")
            cfg.password = os.environ.get("OS_PASSWORD", "")
            cfg.user_id = os.environ.get("OS_USER_ID", "")
            cfg.user_domain_id = os.environ.get("OS_USER_DOMAIN_ID", "")
            cfg.user_domain_name = os.environ.get("OS_USER_DOMAIN_NAME", "Default")
            cfg.project_id = os.environ.get("OS_PROJECT_ID", "") or os.environ.get("OS_TENANT_ID", "")
            cfg.project_name = os.environ.get("OS_PROJECT_NAME", "") or os.environ.get("OS_TENANT_NAME", "")
            cfg.project_domain_id = os.environ.get("OS_PROJECT_DOMAIN_ID", "")
            cfg.project_domain_name = os.environ.get("OS_PROJECT_DOMAIN_NAME", "Default")
            cfg.region = os.environ.get("OS_REGION_NAME", "")
            cfg.auth_type = os.environ.get("OS_AUTH_TYPE", "v3password")
        return cfg

    def auth_body(self) -> dict:
        if self.auth_source == "openstack":
            if self.auth_type not in {"password", "v3password"}:
                raise ValueError("Поддерживается RC с OS_AUTH_TYPE=password/v3password")
            missing = []
            if not (self.user_id or self.username):
                missing.append("OS_USERNAME или OS_USER_ID")
            if not self.password:
                missing.append("OS_PASSWORD")
            if not (self.project_id or self.project_name):
                missing.append("OS_PROJECT_ID или OS_PROJECT_NAME")
            if missing:
                raise ValueError("Задайте переменные RC: " + ", ".join(missing))
            user = {"password": self.password}
            if self.user_id:
                user["id"] = self.user_id
            else:
                user.update(name=self.username, domain=(
                    {"id": self.user_domain_id} if self.user_domain_id else {"name": self.user_domain_name}))
            project = ({"id": self.project_id} if self.project_id else {
                "name": self.project_name,
                "domain": ({"id": self.project_domain_id} if self.project_domain_id else
                           {"name": self.project_domain_name}),
            })
        else:
            missing = [key for key, value in (("SELECTEL_ACCOUNT_ID", self.account_id),
                       ("SELECTEL_USERNAME", self.username), ("SELECTEL_PASSWORD", self.password)) if not value]
            if missing:
                raise ValueError("Задайте переменные: " + ", ".join(missing))
            user = {"name": self.username, "domain": {"name": self.account_id}, "password": self.password}
            project = {"name": self.project_name, "domain": {"name": self.account_id}}
        return {"auth": {"identity": {"methods": ["password"], "password": {"user": user}},
                         "scope": {"project": project}}}


class SelectelClient:
    """Тонкий клиент OpenStack API Selectel (Nova compute)."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.token: str | None = None
        self.project_id: str | None = None
        self.compute_url: str | None = None
        self.catalog: list[dict] = []

    # --- auth -----------------------------------------------------------

    def _auth(self) -> None:
        import requests  # ленивый импорт: mock-режим работает без зависимостей

        r = requests.post(f"{self.cfg.auth_url.rstrip('/')}/auth/tokens", json=self.cfg.auth_body(), timeout=30)
        if r.status_code >= 400:
            raise SelectelAPIError(r.status_code, f"Selectel auth failed ({r.status_code})")
        self.token = r.headers["X-Subject-Token"]  # токен приходит в заголовке
        data = r.json()
        self.project_id = data["token"]["project"]["id"]
        self.catalog = data["token"].get("catalog", [])
        self.compute_url = self._compute_endpoint(self.catalog)
        log.info("token ok, project=%s, compute=%s", self.project_id, self.compute_url)

    def _compute_endpoint(self, catalog: list[dict]) -> str:
        return self._endpoint("compute", catalog)

    def _endpoint(self, service_type: str, catalog: list[dict] | None = None) -> str:
        candidates: list[str] = []
        for entry in self.catalog if catalog is None else catalog:
            if entry.get("type") != service_type:
                continue
            for ep in entry.get("endpoints", []):
                if self.cfg.region and ep.get("region") != self.cfg.region:
                    continue
                if ep.get("url") and ep.get("interface") == "public":
                    candidates.append(ep["url"])
        if not candidates:
            raise RuntimeError(f"Публичный {service_type} endpoint для выбранного региона не найден в service catalog")
        url = candidates[0]
        # у некоторых провайдеров в URL есть плейсхолдер проекта
        return url.replace("{project_id}", self.project_id).replace("%(tenant_id)s", self.project_id).rstrip("/")

    def _request(self, method: str, path: str, **kw) -> dict:
        import requests

        if not self.token:
            self._auth()
        url = path if path.startswith("http") else f"{self.compute_url}{path}"
        headers = {"X-Auth-Token": self.token, "Content-Type": "application/json"}
        headers["OpenStack-API-Version"] = "compute 2.72"
        r = requests.request(method, url, headers=headers, timeout=60, **kw)
        if r.status_code == 401:  # токен протух — один ретрай
            self._auth()
            headers["X-Auth-Token"] = self.token
            url = path if path.startswith("http") else f"{self.compute_url}{path}"
            r = requests.request(method, url, headers=headers, timeout=60, **kw)
        if r.status_code >= 400:
            raise SelectelAPIError(r.status_code, f"{method} {path} -> {r.status_code}")
        return r.json() if r.content else {}

    # --- discovery ------------------------------------------------------

    def list_images(self, wanted: str = "") -> list[dict]:
        """Glance v2; the obsolete Nova /images API is absent at 2.72."""
        import requests
        if not self.token:
            self._auth()
        for attempt in range(2):
            base = self._endpoint("image")
            if not base.endswith("/v2"):
                base += "/v2"
            response = requests.get(base + "/images", headers={"X-Auth-Token": self.token},
                                    params={"limit": 100, **({"name": wanted} if wanted else {})}, timeout=30)
            if response.status_code == 401 and attempt == 0:
                self._auth()
                continue
            if response.status_code >= 400:
                raise SelectelAPIError(response.status_code, f"Glance images -> {response.status_code}")
            return response.json().get("images", [])
        raise RuntimeError("Glance authorization failed")

    def get_image(self, image_id: str) -> dict:
        import requests
        image_id = str(uuid.UUID(image_id))
        if not self.token:
            self._auth()
        for attempt in range(2):
            base = self._endpoint("image")
            if not base.endswith("/v2"):
                base += "/v2"
            response = requests.get(base + "/images/" + image_id,
                                    headers={"X-Auth-Token": self.token}, timeout=30)
            if response.status_code == 401 and attempt == 0:
                self._auth()
                continue
            if response.status_code >= 400:
                raise SelectelAPIError(response.status_code, f"Glance image -> {response.status_code}")
            return response.json()
        raise RuntimeError("Glance authorization failed")

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
        if wanted:
            try:
                uuid.UUID(wanted)
            except ValueError:
                pass
            else:
                return self.get_image(wanted)
        images = self.list_images(wanted)
        if wanted:
            for im in images:
                if im["id"] == wanted or wanted == im["name"]:
                    return im
            raise RuntimeError(f"образ '{wanted}' не найден — укажи SELECTEL_IMAGE")
        active = [im for im in images if im.get("status", "").lower() == "active"]
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

    def server_payload(self, name: str, flavor_id: str, image_id: str, key_name: str, user_data: str | None) -> dict:
        server = {
            "name": name,
            "flavorRef": flavor_id,
            "imageRef": image_id,
            "key_name": key_name,
            "networks": ([{"uuid": self.cfg.network_id}] if self.cfg.network_id else
                         [{"name": self.cfg.network}] if self.cfg.network else "auto"),
            "metadata": {"scanbox-job": name},
        }
        if self.cfg.preemptible:
            server["tags"] = ["preemptible"]
        if self.cfg.availability_zone:
            server["availability_zone"] = self.cfg.availability_zone
        if self.cfg.security_group:
            server["security_groups"] = [{"name": self.cfg.security_group}]
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
        return {"server": server}

    def create_server(self, **kwargs) -> dict:
        return self._request("POST", "/servers", json=self.server_payload(**kwargs))["server"]

    def get_server(self, server_id: str) -> dict:
        return self._request("GET", f"/servers/{server_id}")["server"]

    def check_preempted(self, server: dict) -> None:
        if self.cfg.preemptible and server.get("status") in {"EXPIRED", "SHELVED_OFFLOADED"}:
            raise ServerPreempted(f"Прерываемый сервер {server.get('id', '')} остановлен ({server['status']})")

    def wait_active(self, server_id: str, timeout: int = 600, poll: int = 10) -> str:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            server = self.get_server(server_id)
            self.check_preempted(server)
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
        except SelectelAPIError as exc:
            if exc.status_code != 404:
                raise

    def wait_deleted(self, server_id: str, timeout: int = 300, poll: int = 5) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                self.get_server(server_id)
            except SelectelAPIError as exc:
                if exc.status_code == 404:
                    return
                raise
            time.sleep(poll)
        raise TimeoutError(f"Удаление сервера {server_id} не подтверждено за {timeout}с")

    def wait_volumes_deleted(self, volume_ids: list[str], timeout: int = 300, poll: int = 5) -> None:
        """Verify Cinder deletion; never delete an unrelated volume explicitly."""
        import requests
        if not volume_ids:
            return
        if not self.token:
            self._auth()
        deadline = time.monotonic() + timeout
        remaining = set(volume_ids)
        while remaining and time.monotonic() < deadline:
            endpoints = [ep["url"] for service in self.catalog if service.get("type") == "volumev3"
                         for ep in service.get("endpoints", [])
                         if ep.get("interface") == "public" and ep.get("url") and
                         (not self.cfg.region or ep.get("region") == self.cfg.region)]
            if not endpoints:
                raise RuntimeError("volumev3 endpoint не найден: проверьте удаление сетевых дисков вручную")
            base = endpoints[0].replace("{project_id}", self.project_id).replace("%(tenant_id)s", self.project_id)
            for volume_id in list(remaining):
                response = requests.get(f"{base.rstrip('/')}/volumes/{volume_id}",
                                        headers={"X-Auth-Token": self.token}, timeout=30)
                if response.status_code == 404:
                    remaining.remove(volume_id)
                elif response.status_code == 401:
                    self._auth()
                    break
                elif response.status_code >= 400:
                    raise SelectelAPIError(response.status_code, f"Не удалось проверить диск {volume_id}")
            if remaining:
                time.sleep(poll)
        if remaining:
            raise TimeoutError("Удаление дисков не подтверждено: " + ", ".join(sorted(remaining)))


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


def ssh_run_fetch(cfg: Config, key, host: str, photo: Path, out: Path,
                  client=None, server_id: str | None = None) -> None:
    """Залить фото + джобу, выполнить на GPU, забрать GLB."""
    import paramiko

    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    deadline = time.time() + 300
    while True:
        if client is not None and server_id:
            client.check_preempted(client.get_server(server_id))
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
        channel = stdout.channel
        command_deadline = time.monotonic() + 1800
        next_poll = 0.0
        error_tail = b""
        while True:
            while channel.recv_ready():
                channel.recv(65536)
            while channel.recv_stderr_ready():
                error_tail = (error_tail + channel.recv_stderr(65536))[-8192:]
            if channel.exit_status_ready():
                break
            if time.monotonic() >= command_deadline:
                raise TimeoutError("Генерация не закончилась за 1800с")
            if client is not None and server_id and time.monotonic() >= next_poll:
                client.check_preempted(client.get_server(server_id))
                next_poll = time.monotonic() + 10
            time.sleep(0.5)
        exit_code = channel.recv_exit_status()
        err = error_tail.decode(errors="replace")
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
            ssh_run_fetch(cfg, key, ip, photo, out, client=client, server_id=server_id)
            status("generating", "генерация завершена на GPU")

        status("done", f"модель: {out}")
    except Exception as exc:
        status("failed", str(exc))
        if cfg.keep_on_error:
            log.warning(
                "--keep-on-error: сервер НЕ удалён (id=%s) — сними флаг, чтобы гарантировать удаление", server_id
            )
        raise
    finally:
        try:
            if server_id and not cfg.keep_on_error:
                status("deleting", "удаляем сервер ...")
                client.delete_server(server_id)
                if not cfg.mock:
                    client.wait_deleted(server_id)
                status("deleted", "сервер удалён")
        finally:
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
    ap.add_argument("--preemptible", action="store_true", help="создать прерываемую VM Selectel")
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
    cfg.preemptible = cfg.preemptible or args.preemptible
    if args.flavor:
        cfg.flavor = args.flavor
    if args.image:
        cfg.image = args.image

    if not cfg.mock:
        try:
            cfg.auth_body()
        except ValueError as exc:
            log.error("%s", exc)
            return 2

    run_job(cfg, photo, Path(args.out), args.job_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
