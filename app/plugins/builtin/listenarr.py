"""Listenarr tools -- see app/listenarr/client.py's docstring for the
significant confidence caveat: this is the least-verified integration in
this codebase, deliberately scoped to two near-universal Servarr-family
endpoints rather than a guessed library-listing one. Registered only
when SIRISAI_LISTENARR_BASE_URL and SIRISAI_LISTENARR_API_KEY are both set.
"""

from typing import Any

from app.listenarr.client import ListenarrClient
from app.plugins.base import Tool, ToolPermission


class ListenarrSystemStatusTool(Tool):
    name = "listenarr_system_status"
    description = "Get Listenarr's system status (version, health) -- confirms it's reachable and configured."
    permission = ToolPermission.READ
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, client: ListenarrClient) -> None:
        self._client = client

    def run(self, **kwargs: Any) -> Any:
        return self._client.system_status()


class ListenarrQueueTool(Tool):
    name = "listenarr_queue"
    description = "List Listenarr's current download queue."
    permission = ToolPermission.READ
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, client: ListenarrClient) -> None:
        self._client = client

    def run(self, **kwargs: Any) -> Any:
        return self._client.queue()
