"""Sonarr tools -- TV library visibility and search triggering, via
app/sonarr/client.py. Registered only when SIRISAI_SONARR_BASE_URL and
SIRISAI_SONARR_API_KEY are both set. Same scope reasoning as Radarr's
tools (app/plugins/builtin/radarr.py) -- READ for listing/queue,
SAFE_ACTION for triggering a search, nothing that adds or removes series.
"""

from typing import Any

from app.plugins.base import Tool, ToolPermission
from app.sonarr.client import SonarrClient


class SonarrListSeriesTool(Tool):
    name = "sonarr_list_series"
    description = "List TV series in the Sonarr library -- title, year, monitored status."
    permission = ToolPermission.READ
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, client: SonarrClient) -> None:
        self._client = client

    def run(self, **kwargs: Any) -> Any:
        return self._client.list_series()


class SonarrQueueTool(Tool):
    name = "sonarr_queue"
    description = "List Sonarr's current download queue -- what's actively downloading and its progress."
    permission = ToolPermission.READ
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, client: SonarrClient) -> None:
        self._client = client

    def run(self, **kwargs: Any) -> Any:
        return self._client.queue()


class SonarrSearchSeriesTool(Tool):
    name = "sonarr_search_series"
    description = (
        "Trigger a background search for every monitored episode of one series already in the "
        "Sonarr library, using its series_id from sonarr_list_series. Queues the search -- "
        "doesn't wait for or guarantee a result."
    )
    permission = ToolPermission.SAFE_ACTION
    parameters = {
        "type": "object",
        "properties": {"series_id": {"type": "integer", "description": "The Sonarr series id to search for."}},
        "required": ["series_id"],
    }

    def __init__(self, client: SonarrClient) -> None:
        self._client = client

    def run(self, series_id: int = 0, **kwargs: Any) -> Any:
        return self._client.search_series(series_id)
