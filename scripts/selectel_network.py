"""Neutron networking for a private-network VM with a temporary floating IP."""
from __future__ import annotations

import ipaddress
import time

from scripts.orchestrator import SelectelAPIError


class NeutronClient:
    def __init__(self, identity):
        self.identity = identity

    def request(self, method, path, **kwargs):
        import requests
        if not self.identity.token:
            self.identity._auth()
        for attempt in range(2):
            base = self.identity._endpoint("network")
            if not base.endswith("/v2.0"):
                base += "/v2.0"
            response = requests.request(method, base + path,
                headers={"X-Auth-Token": self.identity.token, "Content-Type": "application/json"},
                timeout=30, **kwargs)
            if response.status_code == 401 and attempt == 0:
                self.identity._auth()
                continue
            if response.status_code >= 400:
                raise SelectelAPIError(response.status_code,
                    f"Neutron {method} {path} -> {response.status_code}")
            return response.json() if response.content else {}
        raise RuntimeError("Neutron authorization failed")

    def validate_topology(self, network_id, external_id):
        if not network_id or network_id == external_id:
            raise ValueError("SELECTEL_NETWORK_ID должен указывать приватную сеть; "
                             "SELECTEL_FLOATING_NETWORK_ID — внешнюю")
        network = self.request("GET", f"/networks/{network_id}")["network"]
        external = self.request("GET", f"/networks/{external_id}")["network"]
        if network.get("router:external"):
            raise ValueError("Для floating IP выберите приватную сеть VM")
        if network.get("port_security_enabled") is not True:
            raise ValueError("В приватной сети требуется port_security_enabled=true")
        if external.get("router:external") is not True:
            raise ValueError("SELECTEL_FLOATING_NETWORK_ID не является внешней сетью")
        ports = self.request("GET", "/ports", params={"network_id": network_id}).get("ports", [])
        router_ids = {port.get("device_id") for port in ports
                      if "router" in port.get("device_owner", "") and
                      "interface" in port.get("device_owner", "")}
        routers = self.request("GET", "/routers",
            params={"project_id": self.identity.project_id}).get("routers", [])
        connected = [router for router in routers if router.get("id") in router_ids and
                     (router.get("external_gateway_info") or {}).get("network_id") == external_id and
                     router.get("admin_state_up") is not False]
        if not connected:
            raise ValueError("Не найден активный роутер между приватной и внешней сетью")
        return {"network_id": network_id, "external_network_id": external_id,
                "router_id": connected[0]["id"], "resources_created": False}

    def server_port(self, server_id, network_id):
        ports = self.request("GET", "/ports",
            params={"device_id": server_id, "network_id": network_id}).get("ports", [])
        candidates = []
        for port in ports:
            if port.get("device_id") != server_id or port.get("network_id") != network_id:
                continue
            for fixed in port.get("fixed_ips", []):
                address = fixed.get("ip_address", "")
                try:
                    is_ipv4 = ipaddress.ip_address(address).version == 4
                except ValueError:
                    continue
                if is_ipv4:
                    candidates.append((port["id"], address))
        if len(candidates) != 1:
            raise ValueError("Для VM требуется ровно один IPv4-порт в выбранной приватной сети")
        return candidates[0]

    def create_floating_ip(self, external_id, port_id, private_ip, description):
        # No automatic retry: an interrupted POST can still allocate a billable IP.
        return self.request("POST", "/floatingips", json={"floatingip": {
            "floating_network_id": external_id, "port_id": port_id,
            "fixed_ip_address": private_ip, "description": description}})["floatingip"]

    def delete_floating_ip(self, floating_id):
        try:
            self.request("DELETE", f"/floatingips/{floating_id}")
        except SelectelAPIError as exc:
            if exc.status_code != 404:
                raise

    def wait_deleted(self, floating_id, timeout=180, poll=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                self.request("GET", f"/floatingips/{floating_id}")
            except SelectelAPIError as exc:
                if exc.status_code == 404:
                    return
                raise
            time.sleep(poll)
        raise TimeoutError(f"Удаление floating IP {floating_id} не подтверждено")
