"""Radarr tools -- movie library visibility and search triggering, via
app/radarr/client.py. Registered only when SIRISAI_RADARR_BASE_URL and
SIRISAI_RADARR_API_KEY are both set.

Deliberately narrow, same "GET-only where it matters, one routine action"
scope as this codebase's other library-manager tools: listing/queue are
READ, triggering a search is SAFE_ACTION (routine and reversible -- it
just queues a background search, doesn't remove or overwrite anything).
Adding new movies or deleting existing ones isn't built here -- a bigger,
more consequential action (quality profile choice, disk space) that
deserves its own careful design later, not folded in as a side effect of
"add Radarr support".
"""

from typing import Any

from app.plugins.base import Tool, ToolPermission
from app.radarr.client import RadarrClient


class RadarrListMoviesTool(Tool):
    name = "radarr_list_movies"
    description = "List movies in the Radarr library -- title, year, monitored/download status."
    permission = ToolPermission.READ
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, client: RadarrClient) -> None:
        self._client = client

    def run(self, **kwargs: Any) -> Any:
        return self._client.list_movies()


class RadarrQueueTool(Tool):
    name = "radarr_queue"
    description = "List Radarr's current download queue -- what's actively downloading and its progress."
    permission = ToolPermission.READ
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, client: RadarrClient) -> None:
        self._client = client

    def run(self, **kwargs: Any) -> Any:
        return self._client.queue()


class RadarrSearchMovieTool(Tool):
    name = "radarr_search_movie"
    description = (
        "Trigger a background search for one movie already in the Radarr library, using its "
        "movie_id from radarr_list_movies. Queues the search -- doesn't wait for or guarantee a result."
    )
    permission = ToolPermission.SAFE_ACTION
    parameters = {
        "type": "object",
        "properties": {"movie_id": {"type": "integer", "description": "The Radarr movie id to search for."}},
        "required": ["movie_id"],
    }

    def __init__(self, client: RadarrClient) -> None:
        self._client = client

    def run(self, movie_id: int = 0, **kwargs: Any) -> Any:
        return self._client.search_movie(movie_id)
