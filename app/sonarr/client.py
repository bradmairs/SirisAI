"""Thin client for Sonarr's v3 API (https://sonarr.tv/docs/api/) -- TV
collection management. Same Servarr-family API shape as Radarr
(app/radarr/client.py): X-Api-Key header auth, /api/v3/* REST resources,
POST /api/v3/command to trigger background work.

lookup_series()/list_root_folders()/list_quality_profiles()/add_series()/
delete_series()/calendar() (a hardening pass) mirror Radarr's own
additions -- see app/plugins/builtin/sonarr.py for the tool-level
CONFIRM_REQUIRED gating on add/delete. Episode-level monitoring (e.g.
"only get season 2") isn't built here -- search_series() already
searches every monitored episode of a series, which covers the common
"get me this show" ask; per-episode granularity is a separate, smaller
feature this milestone doesn't need.
"""

from datetime import UTC, datetime, timedelta

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

    def lookup_series(self, term: str) -> list[dict]:
        """Searches TVDB (via Sonarr's own proxy for it) for candidates to
        add -- not a library search. The result's tvdb_id/title feed
        directly into add_series()."""
        response = self._client.get("/api/v3/series/lookup", params={"term": term})
        response.raise_for_status()
        return [
            {"tvdb_id": s.get("tvdbId"), "title": s.get("title"), "year": s.get("year"), "overview": s.get("overview")}
            for s in response.json()
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

    def add_series(
        self,
        tvdb_id: int,
        title: str,
        quality_profile_id: int,
        root_folder_path: str,
        monitored: bool = True,
        search_now: bool = True,
    ) -> dict:
        payload = {
            "tvdbId": tvdb_id,
            "title": title,
            "qualityProfileId": quality_profile_id,
            "rootFolderPath": root_folder_path,
            "monitored": monitored,
            "seasonFolder": True,
            "addOptions": {"searchForMissingEpisodes": search_now},
        }
        response = self._client.post("/api/v3/series", json=payload)
        response.raise_for_status()
        added = response.json()
        return {
            "id": added.get("id"),
            "title": added.get("title"),
            "year": added.get("year"),
            "monitored": added.get("monitored"),
        }

    def delete_series(self, series_id: int, delete_files: bool = False) -> dict:
        response = self._client.delete(
            f"/api/v3/series/{series_id}", params={"deleteFiles": "true" if delete_files else "false"}
        )
        response.raise_for_status()
        return {"deleted": True, "series_id": series_id, "files_deleted": delete_files}

    def calendar(self, days_ahead: int = 7) -> list[dict]:
        start = datetime.now(UTC)
        end = start + timedelta(days=days_ahead)
        response = self._client.get(
            "/api/v3/calendar",
            params={"start": start.date().isoformat(), "end": end.date().isoformat(), "includeSeries": "true"},
        )
        response.raise_for_status()
        return [
            {
                "series_title": e.get("series", {}).get("title"),
                "episode_title": e.get("title"),
                "season_number": e.get("seasonNumber"),
                "episode_number": e.get("episodeNumber"),
                "air_date": e.get("airDate"),
            }
            for e in response.json()
        ]

    def close(self) -> None:
        self._client.close()
