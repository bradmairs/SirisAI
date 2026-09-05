"""Thin client for Bazarr's API (https://wiki.bazarr.media/API/Using-the-API/)
-- subtitle management for Radarr/Sonarr libraries. Header-based API key
auth (X-API-KEY), same convention as this codebase's other integrations,
though Bazarr's own API is less exhaustively covered by the same
scrutiny as Radarr/Sonarr's here -- if the header name or response
shape ever turns out to differ, this is the one file to adjust.

search_wanted_subtitles() (a hardening pass) runs Bazarr's own built-in
scheduled task by name (POST /api/system/tasks?taskid=...) rather than
searching one item at a time -- Bazarr's per-item search endpoints
(POST /api/movies/subtitles, /api/episodes/subtitles) need a specific
provider/language selection this codebase doesn't have enough verified
detail on yet, so this sticks to the one whole-library action that's
simple enough to be confident about: the same task Bazarr's own UI runs
on its "search wanted" button.
"""

import httpx


class BazarrClient:
    def __init__(
        self, base_url: str, api_key: str, timeout: float = 10.0, transport: httpx.BaseTransport | None = None
    ) -> None:
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"), timeout=timeout, headers={"X-API-KEY": api_key}, transport=transport
        )

    def wanted_movies(self) -> list[dict]:
        response = self._client.get("/api/movies/wanted")
        response.raise_for_status()
        return [
            {"title": r.get("title"), "missing_subtitles": r.get("missing_subtitles")}
            for r in response.json().get("data", [])
        ]

    def wanted_episodes(self) -> list[dict]:
        response = self._client.get("/api/episodes/wanted")
        response.raise_for_status()
        return [
            {
                "series_title": r.get("seriesTitle"),
                "episode_title": r.get("episode_title") or r.get("title"),
                "missing_subtitles": r.get("missing_subtitles"),
            }
            for r in response.json().get("data", [])
        ]

    def search_wanted_subtitles(self) -> dict:
        """Triggers Bazarr's built-in "search wanted subtitles" scheduled
        task for the whole library -- fire-and-forget, same "queued, not
        run synchronously" shape as Radarr's search_movie()/Sonarr's
        search_series()."""
        response = self._client.post("/api/system/tasks", params={"taskid": "search_wanted_subtitles"})
        response.raise_for_status()
        return {"triggered": True}

    def close(self) -> None:
        self._client.close()
