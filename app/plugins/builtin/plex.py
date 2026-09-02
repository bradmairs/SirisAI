"""Plex tools -- what's playing and what libraries exist, via
app/plex/client.py. Registered only when SIRISAI_PLEX_BASE_URL and
SIRISAI_PLEX_TOKEN are both set. Find your token at
https://support.plex.tv/articles/204059436.
"""

from typing import Any

from app.plex.client import PlexClient
from app.plugins.base import Tool, ToolPermission


class PlexNowPlayingTool(Tool):
    name = "plex_now_playing"
    description = "List what's currently playing on Plex -- title, user, playback state, progress."
    permission = ToolPermission.READ
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, client: PlexClient) -> None:
        self._client = client

    def run(self, **kwargs: Any) -> Any:
        return self._client.now_playing()


class PlexLibrarySectionsTool(Tool):
    name = "plex_library_sections"
    description = "List Plex library sections (Movies, TV Shows, Music, etc)."
    permission = ToolPermission.READ
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, client: PlexClient) -> None:
        self._client = client

    def run(self, **kwargs: Any) -> Any:
        return self._client.library_sections()
