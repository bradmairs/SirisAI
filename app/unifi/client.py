"""Thin client for a local UniFi controller's own "Integration API"
(API-key auth, generated locally: the controller's own UI at its LAN IP
-> Settings -> Control Plane -> Integrations -> Create API Key), per
developer.ui.com/network/integration.

Not the same key or product as UniFi's cloud "Site Manager API"
(https://api.ui.com, generated at unifi.ui.com -> Settings -> API):
that cloud key 401'd against this local API when first tried, and a
genuine local key was needed instead (live-tested 2026-09, both against
a real Cloud Gateway Ultra console). The cloud API is more limited too
-- devices only, no live client list -- so the local API is strictly
better when it's reachable, which it normally is for a homelab setup
running SirisAI on the same LAN.

Site scoping is real but indirect: the per-site endpoints below need the
site's internal id (a UUID, e.g. from GET /sites), not the human-readable
name/reference SIRISAI_UNIFI_SITE is configured with (default "default")
-- passing that name directly in the URL 400s. Resolved once via
`_resolve_site_id()` and cached for the client's lifetime.

This was kept read-only through milestone 57 for exactly that reason --
exact paths can shift across controller/UniFi OS versions, and there was
no real controller available to re-verify a write action against. It
stays the least-verified integration in this tool library; block/
unblock/reconnect_client (later milestones, at explicit user request to
extend this integration despite that unresolved gap) model the
Integration API's action-endpoint shape from its published docs
(developer.ui.com/network/integration) rather than a live-tested one --
a path or payload mismatch still fails loudly (404/401/400) rather than
doing anything destructive, same as every read call here already did,
but "loudly" is the only real safety net until someone can point this
at a real controller and confirm the actual wire behavior.

restart_device() (a further milestone, again at explicit user request
after the same blast-radius warning was raised twice) is the highest-
consequence action in this file: unlike a client action, which affects
one device's own connection, restarting network infrastructure (an AP,
switch, or -- worst case -- the site's gateway/router) can disrupt
everyone on that segment, potentially including SirisAI's own
connectivity to the UniFi controller it's calling, for the length of a
full hardware reboot (on the order of a minute or more, not the few
seconds a Docker container restart takes). See
`UniFiRestartDeviceTool`'s own docstring (app/plugins/builtin/unifi.py)
for how that's reflected in its permission tier and its deliberate lack
of `verify()`.
"""

import time

import httpx


class UniFiClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        site: str = "default",
        timeout: float = 10.0,
        verify_ssl: bool = True,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._site = site
        self._site_id: str | None = None
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            headers={"X-API-KEY": api_key, "Accept": "application/json"},
            verify=verify_ssl,
            transport=transport,
        )

    def _resolve_site_id(self) -> str:
        if self._site_id is not None:
            return self._site_id
        response = self._client.get(
            "/proxy/network/integration/v1/sites", params={"limit": 200}
        )
        response.raise_for_status()
        sites = self._unwrap(response.json())
        for entry in sites:
            if (
                entry.get("internalReference") == self._site
                or entry.get("name") == self._site
            ):
                self._site_id = entry["id"]
                return self._site_id
        known = [entry.get("internalReference") or entry.get("name") for entry in sites]
        raise ValueError(f"UniFi site '{self._site}' not found (known sites: {known})")

    def list_devices(self) -> list[dict]:
        site_id = self._resolve_site_id()
        response = self._client.get(
            f"/proxy/network/integration/v1/sites/{site_id}/devices",
            params={"limit": 200},
        )
        response.raise_for_status()
        items = self._unwrap(response.json())
        return [
            {
                "id": item.get("id"),  # needed by restart_device()
                "name": item.get("name"),
                "model": item.get("model"),
                "state": item.get("state"),
            }
            for item in items
        ]

    def list_clients(self) -> list[dict]:
        site_id = self._resolve_site_id()
        response = self._client.get(
            f"/proxy/network/integration/v1/sites/{site_id}/clients",
            params={"limit": 200},
        )
        response.raise_for_status()
        items = self._unwrap(response.json())
        return [
            {
                "id": item.get("id"),  # needed by block_client()/unblock_client()
                "name": item.get("name") or item.get("hostname"),
                "ip_address": item.get("ipAddress") or item.get("ip"),
                "connected_at": item.get("connectedAt"),
                "blocked": item.get("blocked"),
            }
            for item in items
        ]

    def block_client(self, client_id: str) -> dict:
        """Disconnects a client and prevents it reconnecting, via the
        Integration API's action endpoint (POST .../clients/{id}/actions
        with {"action": "BLOCK"}) -- the same action-based shape
        developer.ui.com/network/integration documents for device
        actions (e.g. restart), applied here to clients. Not live-tested
        against a real controller (see this module's docstring)."""
        site_id = self._resolve_site_id()
        response = self._client.post(
            f"/proxy/network/integration/v1/sites/{site_id}/clients/{client_id}/actions",
            json={"action": "BLOCK"},
        )
        response.raise_for_status()
        return {"blocked": True, "client_id": client_id}

    def unblock_client(self, client_id: str) -> dict:
        """Inverse of block_client() -- {"action": "UNBLOCK"} to the same
        endpoint."""
        site_id = self._resolve_site_id()
        response = self._client.post(
            f"/proxy/network/integration/v1/sites/{site_id}/clients/{client_id}/actions",
            json={"action": "UNBLOCK"},
        )
        response.raise_for_status()
        return {"blocked": False, "client_id": client_id}

    def reconnect_client(self, client_id: str) -> dict:
        """Forces a client to disconnect and immediately re-associate --
        {"action": "RECONNECT"} to the same action endpoint as block/
        unblock. Lower-consequence than block_client(): the client is
        never left unable to connect, just cycled, the same kind of
        thing unplugging and replugging a device's WiFi would do -- for
        troubleshooting a flaky connection rather than restricting
        access. Same not-live-tested caveat as block/unblock_client()."""
        site_id = self._resolve_site_id()
        response = self._client.post(
            f"/proxy/network/integration/v1/sites/{site_id}/clients/{client_id}/actions",
            json={"action": "RECONNECT"},
        )
        response.raise_for_status()
        return {"reconnected": True, "client_id": client_id}

    def restart_device(self, device_id: str) -> dict:
        """Reboots a UniFi device -- {"action": "RESTART"} to the
        device-scoped action endpoint (POST .../devices/{id}/actions),
        the same action-endpoint shape as the client actions above. See
        this module's docstring for why this is the highest-consequence
        write here."""
        site_id = self._resolve_site_id()
        response = self._client.post(
            f"/proxy/network/integration/v1/sites/{site_id}/devices/{device_id}/actions",
            json={"action": "RESTART"},
        )
        response.raise_for_status()
        return {"restarted": True, "device_id": device_id}

    def ips_events(self, hours: int = 24) -> list[dict]:
        """Intrusion detection/prevention alerts from the controller's classic
        API (the Integration API has none). Works where the controller
        accepts the API key there too (UniFi OS); raises otherwise, and the
        security briefing reports it as unavailable."""
        end = int(time.time() * 1000)
        response = self._client.post(
            f"/proxy/network/api/s/{self._site}/stat/ips/event",
            json={"start": end - hours * 3600 * 1000, "end": end, "_limit": 200},
        )
        response.raise_for_status()
        return [
            {
                "time": e.get("datetime") or e.get("timestamp"),
                "signature": e.get("inner_alert_signature") or e.get("msg"),
                "category": e.get("inner_alert_category") or e.get("catname"),
                "source": e.get("src_ip"),
                "destination": e.get("dest_ip"),
                "action": e.get("inner_alert_action"),
            }
            for e in self._unwrap(response.json())
        ]

    def set_blocked(self, mac: str, blocked: bool) -> dict:
        """Block or unblock a client by MAC through the classic stamgr command
        (SirisOS 5.3's network alerts). Unlike the Integration API's actions,
        it works for a client that isn't connected right now, so a device
        blocked earlier can be let back in."""
        response = self._client.post(f"/proxy/network/api/s/{self._site}/cmd/stamgr",
                                     json={"cmd": "block-sta" if blocked else "unblock-sta", "mac": mac.lower()})
        response.raise_for_status()
        return {"mac": mac.lower(), "blocked": blocked}

    def traffic(self) -> dict:
        """Live traffic for SirisOS's house view (2.76): the gateway's WAN
        rates, each UniFi device (gateway, switches, access points) and each
        client, in bytes per second, with what each connects through. Uses
        the classic stat endpoints (stat/health, stat/device, stat/sta),
        which carry the rates; the Integration API lists don't."""
        base = f"/proxy/network/api/s/{self._site}"

        def get(path: str) -> list[dict]:
            response = self._client.get(base + path)
            response.raise_for_status()
            return self._unwrap(response.json())

        rate = lambda item, key: float(item.get(key) or 0)  # noqa: E731
        kinds = {"ugw": "gateway", "udm": "gateway", "uxg": "gateway", "usw": "switch", "uap": "access_point"}
        raw_devices = get("/stat/device")
        gateway = next((d for d in raw_devices if kinds.get(str(d.get("type"))) == "gateway"), {})

        # WAN: the gateway's own wan1/wan2 records hold a steady rate. stat/health's "wan"
        # subsystem (and the gateway's `uplink`) read 0 between the controller's refreshes on a
        # Cloud Gateway Ultra, so they're only the fallback, e.g. for older gateways.
        health_wan = next((h for h in get("/stat/health") if h.get("subsystem") == "wan"), {})
        links = [w for w in (gateway.get("wan1"), gateway.get("wan2"))
                 if isinstance(w, dict) and ("rx_bytes-r" in w or "tx_bytes-r" in w)]
        if links:
            wan_rx, wan_tx = sum(rate(w, "rx_bytes-r") for w in links), sum(rate(w, "tx_bytes-r") for w in links)
        else:
            wan_rx, wan_tx = rate(health_wan, "rx_bytes-r"), rate(health_wan, "tx_bytes-r")

        devices = [{
            "mac": d.get("mac"), "name": d.get("name") or d.get("model"), "model": d.get("model"),
            "type": kinds.get(str(d.get("type")), str(d.get("type") or "")),
            "state": "online" if d.get("state") == 1 else "offline",
            "uplink_mac": (d.get("uplink") or {}).get("uplink_mac"),
            "rx_bps": rate(d, "rx_bytes-r"), "tx_bps": rate(d, "tx_bytes-r"),
        } for d in raw_devices]
        clients = [{
            "mac": c.get("mac"), "name": c.get("name") or c.get("hostname") or c.get("oui") or c.get("mac"),
            "ip": c.get("ip"), "wired": bool(c.get("is_wired")),
            "uplink_mac": c.get("sw_mac") if c.get("is_wired") else c.get("ap_mac"),
            "rx_bps": rate(c, "wired-rx_bytes-r" if c.get("is_wired") else "rx_bytes-r"),
            "tx_bps": rate(c, "wired-tx_bytes-r" if c.get("is_wired") else "tx_bytes-r"),
        } for c in get("/stat/sta")]
        self._fill_device_rates(devices, clients, (wan_rx, wan_tx))
        return {"wan": {"rx_bps": wan_rx, "tx_bps": wan_tx, "isp": health_wan.get("isp_name")},
                "devices": devices, "clients": clients}

    @staticmethod
    def _fill_device_rates(devices: list[dict], clients: list[dict], wan: tuple[float, float]) -> None:
        """Devices that don't report their own rate (on a Cloud Gateway Ultra, none of them
        do: the figures are spread over port tables) get the traffic flowing through them,
        which is what a link in the house view carries: the gateway gets the WAN rate, and a
        switch or access point gets the sum of its clients and of the devices below it."""
        by_mac = {d["mac"]: d for d in devices if d.get("mac")}
        below: dict[str, list[dict]] = {}
        for item in [*devices, *clients]:
            if item.get("uplink_mac") in by_mac and item.get("uplink_mac") != item.get("mac"):
                below.setdefault(item["uplink_mac"], []).append(item)
        done: dict[str, tuple[float, float]] = {}

        def through(device: dict, seen: frozenset[str]) -> tuple[float, float]:
            mac = device["mac"]
            if mac in done:
                return done[mac]
            if device["rx_bps"] or device["tx_bps"]:
                rates = (device["rx_bps"], device["tx_bps"])
            elif device["type"] == "gateway":
                rates = wan
            else:
                rx = tx = 0.0
                for item in below.get(mac, []):
                    if item.get("mac") in by_mac:
                        if item["mac"] in seen:   # a loop in reported uplinks: count it once
                            continue
                        item_rx, item_tx = through(item, seen | {mac})
                    else:
                        item_rx, item_tx = item["rx_bps"], item["tx_bps"]
                    rx, tx = rx + item_rx, tx + item_tx
                rates = (rx, tx)
            done[mac] = rates
            return rates

        for device in by_mac.values():
            device["rx_bps"], device["tx_bps"] = through(device, frozenset({device["mac"]}))

    @staticmethod
    def _unwrap(data: object) -> list[dict]:
        """The Integration API wraps list responses as {"data": [...]}."""
        if isinstance(data, dict):
            return data.get("data", [])
        return data if isinstance(data, list) else []

    def close(self) -> None:
        self._client.close()
