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
                    # 7.17: the server owner's own session (Plex gives the owner account id 1)
                    "owner": str(session.get("User", {}).get("id")) == "1",
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

    @staticmethod
    def _item(m: dict) -> dict:
        kind = m.get("type")
        show = m.get("grandparentTitle") if kind == "episode" else m.get("parentTitle") if kind == "season" else None
        label = m.get("title")
        if kind == "episode" and m.get("parentIndex") is not None and m.get("index") is not None:
            label = f"S{int(m['parentIndex']):02d}E{int(m['index']):02d} {m.get('title') or ''}".strip()
        return {
            "title": f"{show}: {label}" if show else label,
            "type": kind,
            "show": show,
            "year": m.get("year"),
            "genres": [g.get("tag") for g in m.get("Genre") or [] if g.get("tag")],
            "rating": m.get("audienceRating") or m.get("rating"),
            "watched": bool(m.get("viewCount")) or (kind == "season" and m.get("viewedLeafCount") == m.get("leafCount")),
            "added_at": m.get("addedAt"),
            "summary": (m.get("summary") or "")[:200],
        }

    def on_deck(self) -> list[dict]:
        """What the owner is part-way through (Continue Watching), 7.17."""
        response = self._client.get("/library/onDeck")
        response.raise_for_status()
        return [self._item(m) for m in response.json().get("MediaContainer", {}).get("Metadata", []) or []]

    def recently_added(self, limit: int = 50) -> list[dict]:
        """Newest additions across the libraries; `watched` is the owner's view count, 7.17."""
        response = self._client.get(
            "/library/recentlyAdded", params={"X-Plex-Container-Start": 0, "X-Plex-Container-Size": limit}
        )
        response.raise_for_status()
        return [self._item(m) for m in response.json().get("MediaContainer", {}).get("Metadata", []) or []]

    def close(self) -> None:
        self._client.close()
