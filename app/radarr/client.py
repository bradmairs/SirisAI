"""Thin client for Radarr's v3 API (https://radarr.video/docs/api/) --
movie collection management. Same Servarr-family API shape as Sonarr
(app/sonarr/client.py): X-Api-Key header auth, /api/v3/* REST resources,
POST /api/v3/command to trigger background work.
"""

import httpx


class RadarrClient:
    def __init__(
        self, base_url: str, api_key: str, timeout: float = 10.0, transport: httpx.BaseTransport | None = None
    ) -> None:
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"), timeout=timeout, headers={"X-Api-Key": api_key}, transport=transport
        )

    def list_movies(self) -> list[dict]:
        response = self._client.get("/api/v3/movie")
        response.raise_for_status()
        return [
            {
                "id": m["id"],
                "title": m.get("title"),
                "year": m.get("year"),
                "monitored": m.get("monitored"),
                "has_file": m.get("hasFile"),
                "status": m.get("status"),
            }
            for m in response.json()
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

    def search_movie(self, movie_id: int) -> dict:
        """Triggers a background search for one movie -- fire-and-forget,
        same as Sonarr's search_series(): the command is queued, not run
        synchronously, so this returns the queued command's own status,
        not a "found it" result."""
        response = self._client.post("/api/v3/command", json={"name": "MoviesSearch", "movieIds": [movie_id]})
        response.raise_for_status()
        return response.json()

    def close(self) -> None:
        self._client.close()
