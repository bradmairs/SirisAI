"""Thin client for Cleanuparr -- stalled/failed download cleanup for
*arr apps and download clients.

EVEN LESS CONFIDENTLY VERIFIED THAN LISTENARR (see
app/listenarr/client.py's docstring): no solid reference documentation
for Cleanuparr's API was available to build against at all, so this is
deliberately limited to a health-check-style status call against a path
convention common to ASP.NET Core apps (Cleanuparr's apparent stack),
rather than guessing at endpoints for its actual cleanup/blocklist
functionality and shipping something that looks more verified than it
is. Once you can point this at a real running instance -- or its
OpenAPI/Swagger spec, if it publishes one, via POST /siris/integrations
(milestone 32) -- that's the way to get real coverage here, not more
hand-written guesses in this file.
"""

import httpx


class CleanuparrClient:
    def __init__(
        self, base_url: str, api_key: str, timeout: float = 10.0, transport: httpx.BaseTransport | None = None
    ) -> None:
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"), timeout=timeout, headers={"X-Api-Key": api_key}, transport=transport
        )

    def health(self) -> dict:
        response = self._client.get("/health")
        response.raise_for_status()
        content_type = response.headers.get("content-type", "")
        if "application/json" in content_type:
            return response.json()
        return {"status": response.text.strip() or "ok"}

    def close(self) -> None:
        self._client.close()
