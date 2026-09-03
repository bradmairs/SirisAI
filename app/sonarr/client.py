"""Thin client for Sonarr's v3 API (https://sonarr.tv/docs/api/) -- TV
collection management. Same Servarr-family API shape as Radarr
(app/radarr/client.py): X-Api-Key header auth, /api/v3/* REST resources,
POST /api/v3/command to trigger background work.
"""

import httpx


class SonarrClient:
    def __init__(
        self, base_url: str, api_key: str, timeout: float = 10.0, transport: httpx.BaseTransport | None = None
    ) -> None:
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"), timeout=timeout, headers={"X-Api-Key": api_key}, transport=transport
        )

    def list_series(self) -> list[dict]:
        response = self._client.get("/api/v3/series")
        response.raise_for_status()
        return [
            {
                "id": s["id"],
                "title": s.get("title"),
                "year": s.get("year"),
                "monitored": s.get("monitored"),
                "status": s.get("status"),
            }
            for s in response.json()
        ]

    def queue(self) -> list[dict]:
        response = self._client.get("/api/v3/queue")
        response.raise_for_status()
        return [
            {
                "title": r.get("title"),
                "status": r.get("status"),
                "timeleft": r.get("timeleft"),
                "size": r.get("size"),
                "sizeleft": r.get("sizeleft"),
            }
            for r in response.json().get("records", [])
        ]

    def search_series(self, series_id: int) -> dict:
        """Triggers a background search for every monitored episode of one
        series -- fire-and-forget, same as Radarr's search_movie(): the
        command is queued, not run synchronously."""
        response = self._client.post("/api/v3/command", json={"name": "SeriesSearch", "seriesId": series_id})
        response.raise_for_status()
        return response.json()

    def close(self) -> None:
        self._client.close()
