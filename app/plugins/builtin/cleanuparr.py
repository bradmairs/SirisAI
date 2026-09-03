"""Cleanuparr tools -- see app/cleanuparr/client.py's docstring: the
least-verified integration here, deliberately limited to a status check.
Registered only when SIRISAI_CLEANUPARR_BASE_URL and
SIRISAI_CLEANUPARR_API_KEY are both set.
"""

from typing import Any

from app.cleanuparr.client import CleanuparrClient
from app.plugins.base import Tool, ToolPermission


class CleanuparrStatusTool(Tool):
    name = "cleanuparr_status"
    description = "Check whether Cleanuparr is reachable and healthy."
    permission = ToolPermission.READ
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, client: CleanuparrClient) -> None:
        self._client = client

    def run(self, **kwargs: Any) -> Any:
        return self._client.health()
