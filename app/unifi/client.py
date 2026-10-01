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

    @staticmethod
    def _unwrap(data: object) -> list[dict]:
        """The Integration API wraps list responses as {"data": [...]}."""
        if isinstance(data, dict):
            return data.get("data", [])
        return data if isinstance(data, list) else []

    def close(self) -> None:
        self._client.close()
