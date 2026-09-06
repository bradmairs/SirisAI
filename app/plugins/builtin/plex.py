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


class PlexSearchTool(Tool):
    name = "plex_search"
    description = (
        "Search everything in the Plex library at once -- movies, TV shows, music, and "
        "anything else -- for a 'find X' request without knowing which library section it's "
        "in. Each result names which library it came from."
    )
    permission = ToolPermission.READ
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Text to search for, e.g. a title or artist name.",
            },
        },
        "required": ["query"],
    }

    def __init__(self, client: PlexClient) -> None:
        self._client = client

    def run(self, query: str = "", **kwargs: Any) -> Any:
        return self._client.search(query)
