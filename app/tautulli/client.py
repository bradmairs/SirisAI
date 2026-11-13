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
                "session_id": s.get("session_id"),  # needed by terminate_session()
                "title": s.get("full_title"),
                "user": s.get("user"),
                "state": s.get("state"),
                "progress_percent": s.get("progress_percent"),
            }
            for s in sessions
        ]

    def recent_history(self, length: int = 10, user: str | None = None) -> list[dict]:
        """`user` (a Plex username) narrows it to what that person watched (6.7)."""
        params: dict[str, Any] = {"length": length}
        if user:
            params["user"] = user
        rows = self._call("get_history", **params).get("data", [])
        return [
            {"title": r.get("full_title"), "user": r.get("user"), "date": r.get("date")} for r in rows
        ]

    def home_stats(self, days: int = 30, count: int = 5) -> list[dict]:
        """Tautulli's own home-page stat cards (most watched movies/shows,
        most active users, ...) -- one entry per category, each with its
        own `rows`. `stat_title` is kept alongside the raw `stat_id`
        (e.g. "top_movies") since it's the human-readable label Tautulli
        itself uses for the same category."""
        categories = self._call("get_home_stats", time_range=days, stats_count=count)
        return [
            {"stat_id": c.get("stat_id"), "title": c.get("stat_title"), "rows": c.get("rows", [])}
            for c in categories
        ]

    def terminate_session(self, session_id: str, message: str = "") -> dict:
        """Stops a currently-playing Plex stream -- consequential (it
        interrupts whoever's watching), gated CONFIRM_REQUIRED at the tool
        level (app/plugins/builtin/tautulli.py). `message` is shown to the
        user in Plex's client as the reason, if given."""
        self._call("terminate_session", session_id=session_id, message=message)
        return {"terminated": True, "session_id": session_id}

    def get_users(self) -> list[dict]:
        """Plex users Tautulli has ever seen -- who's on the server, not
        who's watching right now (current_activity() is that)."""
        users = self._call("get_users")
        return [
            {"user_id": u.get("user_id"), "username": u.get("username"), "friendly_name": u.get("friendly_name")}
            for u in users
        ]

    def owner_username(self) -> str | None:
        """The Plex server owner's username (Tautulli marks them admin): "me" when no
        SIRISAI_PLEX_USERNAME is set."""
        for u in self._call("get_users") or []:
            if str(u.get("is_admin")) in ("1", "True", "true"):
                return u.get("username")
        return None

    def get_libraries(self) -> list[dict]:
        """Per-library item counts, distinct from PlexLibrarySectionsTool
        (app/plugins/builtin/plex.py), which just lists section names --
        this is Tautulli's own tracked stats about each one."""
        libraries = self._call("get_libraries")
        return [
            {
                "section_id": lib.get("section_id"),
                "name": lib.get("section_name"),
                "type": lib.get("section_type"),
                "count": lib.get("count"),
            }
            for lib in libraries
        ]

    def close(self) -> None:
        self._client.close()
