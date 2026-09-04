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

This is the least-verified integration in this tool library: exact paths
can shift across controller/UniFi OS versions. Kept read-only for that
reason -- a path mismatch fails loudly (404/401/400) rather than doing
anything destructive.
"""

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
        response = self._client.get("/proxy/network/integration/v1/sites", params={"limit": 200})
        response.raise_for_status()
        sites = self._unwrap(response.json())
        for entry in sites:
            if entry.get("internalReference") == self._site or entry.get("name") == self._site:
                self._site_id = entry["id"]
                return self._site_id
        known = [entry.get("internalReference") or entry.get("name") for entry in sites]
        raise ValueError(f"UniFi site '{self._site}' not found (known sites: {known})")

    def list_devices(self) -> list[dict]:
        site_id = self._resolve_site_id()
        response = self._client.get(f"/proxy/network/integration/v1/sites/{site_id}/devices", params={"limit": 200})
        response.raise_for_status()
        items = self._unwrap(response.json())
        return [{"name": item.get("name"), "model": item.get("model"), "state": item.get("state")} for item in items]

    def list_clients(self) -> list[dict]:
        site_id = self._resolve_site_id()
        response = self._client.get(f"/proxy/network/integration/v1/sites/{site_id}/clients", params={"limit": 200})
        response.raise_for_status()
        items = self._unwrap(response.json())
        return [
            {
                "name": item.get("name") or item.get("hostname"),
                "ip_address": item.get("ipAddress") or item.get("ip"),
                "connected_at": item.get("connectedAt"),
            }
            for item in items
        ]

    @staticmethod
    def _unwrap(data: object) -> list[dict]:
        """The Integration API wraps list responses as {"data": [...]}."""
        if isinstance(data, dict):
            return data.get("data", [])
        return data if isinstance(data, list) else []

    def close(self) -> None:
        self._client.close()
