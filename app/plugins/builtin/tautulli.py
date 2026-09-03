"""Tautulli tools -- Plex activity/history, via app/tautulli/client.py.
Registered only when SIRISAI_TAUTULLI_BASE_URL and SIRISAI_TAUTULLI_API_KEY
are both set. Generate an API key under Tautulli's Settings -> Web
Interface -> API.
"""

from typing import Any

from app.plugins.base import Tool, ToolPermission
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
    description = "List recent Plex playback history (who watched what, and when) from Tautulli."
    permission = ToolPermission.READ
    parameters = {
        "type": "object",
        "properties": {
            "length": {"type": "integer", "description": "How many recent history entries to return. Defaults to 10."},
        },
        "required": [],
    }

    def __init__(self, client: TautulliClient) -> None:
        self._client = client

    def run(self, length: int = 10, **kwargs: Any) -> Any:
        return self._client.recent_history(length=length)
