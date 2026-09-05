"""Bazarr tools -- what's missing subtitles, via app/bazarr/client.py.
Registered only when SIRISAI_BAZARR_BASE_URL and SIRISAI_BAZARR_API_KEY
are both set. Generate an API key under Bazarr's Settings -> General.

search_wanted_subtitles (a hardening pass) is SAFE_ACTION, same as
Radarr's/Sonarr's search tools -- it queues Bazarr's own background
search, doesn't remove or overwrite anything.
"""

from typing import Any

from app.bazarr.client import BazarrClient
from app.plugins.base import Tool, ToolPermission


class BazarrWantedMoviesTool(Tool):
    name = "bazarr_wanted_movies"
    description = "List movies Bazarr has flagged as missing subtitles."
    permission = ToolPermission.READ
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, client: BazarrClient) -> None:
        self._client = client

    def run(self, **kwargs: Any) -> Any:
        return self._client.wanted_movies()


class BazarrWantedEpisodesTool(Tool):
    name = "bazarr_wanted_episodes"
    description = "List TV episodes Bazarr has flagged as missing subtitles."
    permission = ToolPermission.READ
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, client: BazarrClient) -> None:
        self._client = client

    def run(self, **kwargs: Any) -> Any:
        return self._client.wanted_episodes()


class BazarrSearchWantedSubtitlesTool(Tool):
    name = "bazarr_search_wanted_subtitles"
    description = (
        "Trigger Bazarr to search for all subtitles it's currently missing, across the whole "
        "library. Queues the search -- doesn't wait for or guarantee a result."
    )
    permission = ToolPermission.SAFE_ACTION
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, client: BazarrClient) -> None:
        self._client = client

    def run(self, **kwargs: Any) -> Any:
        return self._client.search_wanted_subtitles()
