"""Thin client for Plex Media Server's API.

Synchronous, same pattern as HomeAssistantClient/DockerClient.
"""

import httpx


class PlexClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        timeout: float = 10.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            headers={"X-Plex-Token": token, "Accept": "application/json"},
            transport=transport,
        )

    def now_playing(self) -> list[dict]:
        response = self._client.get("/status/sessions")
        response.raise_for_status()
        sessions = response.json().get("MediaContainer", {}).get("Metadata", [])
        results = []
        for session in sessions:
            duration = session.get("duration")
            offset = session.get("viewOffset")
            progress_percent = (
                round(100 * offset / duration, 1)
                if duration and offset is not None
                else None
            )
            results.append(
                {
                    "title": session.get("title"),
                    "type": session.get("type"),
                    "user": session.get("User", {}).get("title"),
                    "state": session.get("Player", {}).get("state"),
                    "progress_percent": progress_percent,
                }
            )
        return results

    def library_sections(self) -> list[dict]:
        response = self._client.get("/library/sections")
        response.raise_for_status()
        directories = response.json().get("MediaContainer", {}).get("Directory", [])
        return [
            {"key": d.get("key"), "title": d.get("title"), "type": d.get("type")}
            for d in directories
        ]

    def search(self, query: str) -> list[dict]:
        """Unified search (milestone 71) -- Plex's own `/hubs/search`
        endpoint, the same one its official clients use for "search
        everything," rather than querying each library section
        separately: it already spans movies, TV, music, and anything
        else in the library in one call, grouped into "Hub"s (one per
        media type). Flattened here into one plain list with each item's
        source hub attached as `library`, rather than the model having to
        walk the nested Hub/Metadata structure itself."""
        response = self._client.get("/hubs/search", params={"query": query})
        response.raise_for_status()
        hubs = response.json().get("MediaContainer", {}).get("Hub", [])
        return [
            {
                "title": item.get("title"),
                "type": item.get("type"),
                "year": item.get("year"),
                "library": hub.get("title"),
                "summary": item.get("summary"),
            }
            for hub in hubs
            for item in hub.get("Metadata") or []
        ]

    def close(self) -> None:
        self._client.close()
