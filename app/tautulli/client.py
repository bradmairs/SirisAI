"""Thin client for Tautulli's API (https://github.com/Tautulli/Tautulli/wiki/Tautulli-API-Reference)
-- a Plex activity/history companion, not a replacement for Plex itself
(see app/plex/client.py, which this reads alongside, not instead of).

Auth and every command go through one endpoint (`/api/v2`) with `apikey`
and `cmd` as query params, unlike the header-based auth every other
integration here uses -- Tautulli's own API shape, not a deviation from
this codebase's convention.
"""

from typing import Any

import httpx


class TautulliClient:
    def __init__(
        self, base_url: str, api_key: str, timeout: float = 10.0, transport: httpx.BaseTransport | None = None
    ) -> None:
        self._api_key = api_key
        self._client = httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout, transport=transport)

    def _call(self, cmd: str, **params: Any) -> Any:
        response = self._client.get("/api/v2", params={"apikey": self._api_key, "cmd": cmd, **params})
        response.raise_for_status()
        return response.json()["response"]["data"]

    def current_activity(self) -> list[dict]:
        sessions = self._call("get_activity").get("sessions", [])
        return [
            {
                "title": s.get("full_title"),
                "user": s.get("user"),
                "state": s.get("state"),
                "progress_percent": s.get("progress_percent"),
            }
            for s in sessions
        ]

    def recent_history(self, length: int = 10) -> list[dict]:
        rows = self._call("get_history", length=length).get("data", [])
        return [
            {"title": r.get("full_title"), "user": r.get("user"), "date": r.get("date")} for r in rows
        ]

    def close(self) -> None:
        self._client.close()
