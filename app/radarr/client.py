"""Thin client for Radarr's v3 API (https://radarr.video/docs/api/) --
movie collection management. Same Servarr-family API shape as Sonarr
(app/sonarr/client.py): X-Api-Key header auth, /api/v3/* REST resources,
POST /api/v3/command to trigger background work.

lookup_movie()/list_root_folders()/list_quality_profiles()/add_movie()/
delete_movie()/calendar() (a hardening pass) are the "bigger, more
consequential action" this file's original docstring deliberately
deferred -- see app/plugins/builtin/radarr.py for the tool-level
CONFIRM_REQUIRED gating on add/delete.
"""

from datetime import UTC, datetime, timedelta

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

    def lookup_movie(self, term: str) -> list[dict]:
        """Searches TMDB (via Radarr's own proxy for it) for candidates to
        add -- not a library search, nothing here has to already exist in
        Radarr. The result's tmdb_id/title feed directly into add_movie()."""
        response = self._client.get("/api/v3/movie/lookup", params={"term": term})
        response.raise_for_status()
        return [
            {"tmdb_id": m.get("tmdbId"), "title": m.get("title"), "year": m.get("year"), "overview": m.get("overview")}
            for m in response.json()
        ]

    def list_root_folders(self) -> list[dict]:
        response = self._client.get("/api/v3/rootfolder")
        response.raise_for_status()
        return [
            {
                "id": f["id"],
                "path": f.get("path"),
                "free_space_gb": round(f["freeSpace"] / 1e9, 1) if f.get("freeSpace") is not None else None,
            }
            for f in response.json()
        ]

    def list_quality_profiles(self) -> list[dict]:
        response = self._client.get("/api/v3/qualityprofile")
        response.raise_for_status()
        return [{"id": p["id"], "name": p.get("name")} for p in response.json()]

    def add_movie(
        self,
        tmdb_id: int,
        title: str,
        quality_profile_id: int,
        root_folder_path: str,
        monitored: bool = True,
        search_now: bool = True,
    ) -> dict:
        payload = {
            "tmdbId": tmdb_id,
            "title": title,
            "qualityProfileId": quality_profile_id,
            "rootFolderPath": root_folder_path,
            "monitored": monitored,
            "addOptions": {"searchForMovie": search_now},
        }
        response = self._client.post("/api/v3/movie", json=payload)
        response.raise_for_status()
        added = response.json()
        return {
            "id": added.get("id"),
            "title": added.get("title"),
            "year": added.get("year"),
            "monitored": added.get("monitored"),
        }

    def delete_movie(self, movie_id: int, delete_files: bool = False) -> dict:
        response = self._client.delete(
            f"/api/v3/movie/{movie_id}", params={"deleteFiles": "true" if delete_files else "false"}
        )
        response.raise_for_status()
        return {"deleted": True, "movie_id": movie_id, "files_deleted": delete_files}

    def calendar(self, days_ahead: int = 7) -> list[dict]:
        start = datetime.now(UTC)
        end = start + timedelta(days=days_ahead)
        response = self._client.get(
            "/api/v3/calendar", params={"start": start.date().isoformat(), "end": end.date().isoformat()}
        )
        response.raise_for_status()
        return [
            {
                "title": m.get("title"),
                "in_cinemas": m.get("inCinemas"),
                "physical_release": m.get("physicalRelease"),
                "digital_release": m.get("digitalRelease"),
            }
            for m in response.json()
        ]

    def close(self) -> None:
        self._client.close()
