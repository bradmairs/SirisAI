"""Thin client for Listenarr -- an audiobook/podcast manager.

THE LEAST CONFIDENTLY VERIFIED INTEGRATION IN THIS CODEBASE, more so even
than UniFi (app/unifi/client.py's own docstring holds the previous title).
Listenarr shares the Servarr family's general API convention -- an
X-Api-Key header, /api/v1/* REST resources, the same shape Sonarr/Radarr/
Lidarr/Readarr/Prowlarr all use -- but there was no live instance and no
solid reference documentation available to confirm Listenarr's specific
resource model against (what a library item is called, its exact fields).

Deliberately scoped to only the two endpoints that are near-universal
across every Servarr-family app regardless of what they manage --
system/status and queue -- rather than guessing at a library-listing
endpoint's resource name and risking a plausible-looking but fabricated
integration. Extend this file once it's been checked against a real
Listenarr instance (or consider POST /siris/integrations, milestone 32's
OpenAPI discovery, if Listenarr publishes a spec -- likely, given its
apparent ASP.NET Core heritage).
"""

import httpx


class ListenarrClient:
    def __init__(
        self, base_url: str, api_key: str, timeout: float = 10.0, transport: httpx.BaseTransport | None = None
    ) -> None:
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"), timeout=timeout, headers={"X-Api-Key": api_key}, transport=transport
        )

    def system_status(self) -> dict:
        response = self._client.get("/api/v1/system/status")
        response.raise_for_status()
        return response.json()

    def queue(self) -> list[dict]:
        response = self._client.get("/api/v1/queue")
        response.raise_for_status()
        return response.json().get("records", [])

    def close(self) -> None:
        self._client.close()
