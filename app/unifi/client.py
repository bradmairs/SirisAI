"""Thin client for UniFi's cloud "Site Manager API" (https://api.ui.com,
API-key auth, generated at unifi.ui.com -> Settings -> API). Originally
written against the local self-hosted controller's "Integration API"
(/proxy/network/integration/v1/...) per developer.ui.com, but live-tested
against a real controller (2026-09) and that path 401'd -- this
controller's key only authenticates against the cloud API. Same key,
different product: the Integration API is exposed by the controller
itself; the Site Manager API is UI's own cloud backend, aggregating
every console/site tied to the account. If a future controller's key
turns out to work against the local Integration API instead, that's the
one other shape to support -- this is still the one file to adjust.

This is the least-verified integration in this tool library: exact paths
can shift and there's no local sandbox to test against. Kept read-only
for that reason -- a path mismatch fails loudly (404/401) rather than
doing anything destructive.
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
        # Unused for now -- the Site Manager API scopes by host/site id,
        # not the local controller's site *name*, and a single-home
        # account only ever has the one site anyway. Kept as a
        # constructor param (rather than removed) so a future filtered
        # call (GET /v1/devices?siteIds[]=...) is a small addition, not a
        # signature change every caller has to follow.
        self._site = site
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            headers={"X-API-KEY": api_key, "Accept": "application/json"},
            verify=verify_ssl,
            transport=transport,
        )

    def list_devices(self) -> list[dict]:
        # Response shape: {"data": [{"hostId": ..., "hostName": ...,
        # "devices": [{"name", "model", "status", ...}, ...]}, ...]} --
        # one entry per host/console on the account, each carrying its
        # own device list; flatten into one list across all of them.
        response = self._client.get("/v1/devices")
        response.raise_for_status()
        hosts = self._unwrap(response.json())
        return [
            {"name": item.get("name"), "model": item.get("model"), "state": item.get("status")}
            for host in hosts
            for item in host.get("devices", [])
        ]

    def list_clients(self) -> list[dict]:
        # No equivalent on the Site Manager API -- live client lists
        # (who's currently connected) aren't exposed by UI's cloud
        # backend, only by a local controller's own Integration API.
        # Fail with a clear reason rather than a raw, confusing 404.
        raise NotImplementedError(
            "UniFi's cloud Site Manager API (api.ui.com) has no client-listing endpoint -- "
            "only a local controller's own Integration API exposes live connected clients."
        )

    @staticmethod
    def _unwrap(data: object) -> list[dict]:
        """The Site Manager API wraps list responses as {"data": [...]}."""
        if isinstance(data, dict):
            return data.get("data", [])
        return data if isinstance(data, list) else []

    def close(self) -> None:
        self._client.close()
