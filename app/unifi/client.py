"""Thin client for UniFi's Network "Integration API" (API-key auth) --
https://developer.ui.com. Deliberately not the legacy cookie-based
controller login, which varies too much across controller/UniFi OS
versions to support reliably here.

This is the least-verified integration in this tool library: exact paths
can shift across controller/UniFi OS versions and it couldn't be tested
against a live controller in development. Kept read-only for that reason
-- a path mismatch fails loudly (404/401) rather than doing anything
destructive. If your controller uses a different API shape, this is the
one file to adjust.
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
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            headers={"X-API-KEY": api_key, "Accept": "application/json"},
            verify=verify_ssl,
            transport=transport,
        )

    def list_devices(self) -> list[dict]:
        response = self._client.get(f"/proxy/network/integration/v1/sites/{self._site}/devices")
        response.raise_for_status()
        items = self._unwrap(response.json())
        return [{"name": item.get("name"), "model": item.get("model"), "state": item.get("state")} for item in items]

    def list_clients(self) -> list[dict]:
        response = self._client.get(f"/proxy/network/integration/v1/sites/{self._site}/clients")
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
