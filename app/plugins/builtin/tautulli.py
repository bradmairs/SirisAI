"""Tautulli tools -- Plex activity/history, via app/tautulli/client.py.
Registered only when SIRISAI_TAUTULLI_BASE_URL and SIRISAI_TAUTULLI_API_KEY
are both set. Generate an API key under Tautulli's Settings -> Web
Interface -> API.

home_stats is READ, same as the other listing tools here. terminate_session
(a hardening pass) is CONFIRM_REQUIRED -- unlike everything else in this
file, it has a real, immediate effect on another person's Plex playback,
not just a query.
"""

from typing import Any

from app.plugins.base import Tool, ToolPermission, ToolVerification
from app.tautulli.client import TautulliClient


class TautulliCurrentActivityTool(Tool):
    name = "tautulli_current_activity"
    description = "List what's currently streaming on Plex right now, per Tautulli -- title, user, state, progress."
    permission = ToolPermission.READ
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, client: TautulliClient) -> None:
        self._client = client

    def run(self, **kwargs: Any) -> Any:
        return self._client.current_activity()


class TautulliRecentHistoryTool(Tool):
    name = "tautulli_recent_history"
    description = (
        "List recent Plex playback history (who watched what, and when) from Tautulli. user='me' gives only "
        "what the user themself watched ('what have I watched', 'my watch history'); a Plex username gives that "
        "person's; leave it out for everyone on the server."
    )
    permission = ToolPermission.READ
    parameters = {
        "type": "object",
        "properties": {
            "length": {
                "type": "integer",
                "description": "How many recent history entries to return. Defaults to 10.",
            },
            "user": {"type": "string", "description": "'me' for the user's own history, or a Plex username. Omit for everyone."},
        },
        "required": [],
    }

    def __init__(self, client: TautulliClient, my_username: str = "") -> None:
        self._client = client
        self._me = my_username.strip()

    def run(self, length: int = 10, user: str | None = None, **kwargs: Any) -> Any:
        who = (user or "").strip()
        if who.lower() in ("", "all", "everyone", "anyone"):
            return self._client.recent_history(length=length)
        if who.lower() in ("me", "mine", "my", "myself", "i"):  # 6.69: the user's own, not the whole household's
            who = self._me or self._client.owner_username() or ""
            if not who:
                raise ValueError("I don't know your Plex username: set SIRISAI_PLEX_USERNAME.")
        rows = self._client.recent_history(length=length, user=who)
        return {"user": who, "history": rows}


class TautulliHomeStatsTool(Tool):
    name = "tautulli_home_stats"
    description = (
        "Get Tautulli's home-page stats: most-watched movies/shows, most active users, and similar "
        "categories, over a recent time range."
    )
    permission = ToolPermission.READ
    parameters = {
        "type": "object",
        "properties": {
            "days": {
                "type": "integer",
                "description": "How many days back to summarize. Defaults to 30.",
            },
            "count": {
                "type": "integer",
                "description": "How many rows per category. Defaults to 5.",
            },
        },
        "required": [],
    }

    def __init__(self, client: TautulliClient) -> None:
        self._client = client

    def run(self, days: int = 30, count: int = 5, **kwargs: Any) -> Any:
        return self._client.home_stats(days=days, count=count)


class TautulliTerminateSessionTool(Tool):
    name = "tautulli_terminate_session"
    description = (
        "Stop a currently-playing Plex stream, using its session_id from tautulli_current_activity. "
        "Interrupts whoever's watching -- use only when actually asked to stop a stream."
    )
    permission = ToolPermission.CONFIRM_REQUIRED
    parameters = {
        "type": "object",
        "properties": {
            "session_id": {
                "type": "string",
                "description": "The session to stop, from tautulli_current_activity.",
            },
            "message": {
                "type": "string",
                "description": "Optional message shown to the user in their Plex client.",
            },
        },
        "required": ["session_id"],
    }

    def __init__(self, client: TautulliClient) -> None:
        self._client = client

    def run(self, session_id: str = "", message: str = "", **kwargs: Any) -> Any:
        return self._client.terminate_session(session_id, message=message)

    def verify(self, arguments: dict[str, Any], result: Any) -> ToolVerification:
        """Same "the API call not raising doesn't prove it happened" gap
        this codebase's other verify()s close: Tautulli's own
        terminate_session call is a fire-and-forget request relayed to
        Plex, and the actual player client is what has to honor it --
        a network hiccup or an unresponsive client can leave the stream
        still playing even though Tautulli accepted the command.
        Re-checks current_activity() for the same session_id."""
        session_id = arguments.get("session_id")
        if not session_id:
            raise NotImplementedError(
                f"{self.name} does not support verify without a session_id"
            )

        if any(
            s.get("session_id") == session_id for s in self._client.current_activity()
        ):
            return ToolVerification(
                verified=False, detail=f"Session {session_id} is still active."
            )
        return ToolVerification(
            verified=True, detail=f"Session {session_id} confirmed no longer active."
        )


class TautulliGetUsersTool(Tool):
    name = "tautulli_get_users"
    description = "List Plex users Tautulli has ever seen on this server -- who has access, not who's watching now."
    permission = ToolPermission.READ
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, client: TautulliClient) -> None:
        self._client = client

    def run(self, **kwargs: Any) -> Any:
        return self._client.get_users()


class TautulliGetLibrariesTool(Tool):
    name = "tautulli_get_libraries"
    description = "Get per-library item counts from Tautulli (e.g. how many movies/shows in each library)."
    permission = ToolPermission.READ
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, client: TautulliClient) -> None:
        self._client = client

    def run(self, **kwargs: Any) -> Any:
        return self._client.get_libraries()
