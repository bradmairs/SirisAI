"""Internet speed test tool, via app/speedtest/client.py. Always
registered, like system_info -- no credentials needed, nothing external
to configure, so there's no "unset = don't register" gate to apply.
"""

from typing import Any

from app.plugins.base import Tool, ToolPermission
from app.speedtest.client import SpeedTestClient


class RunSpeedTestTool(Tool):
    name = "run_speed_test"
    description = (
        "Measure real internet speed (download/upload throughput and ping) on this "
        "server's own network connection. Takes 5-25 seconds -- it actually transfers "
        "data to measure real throughput, not a cached or estimated figure."
    )
    # SAFE_ACTION, not READ: unlike every other READ tool here, running
    # this has a real-world side effect (consumes real bandwidth for the
    # duration of the test) even though it's non-destructive and needs no
    # confirmation.
    permission = ToolPermission.SAFE_ACTION
    parameters = {
        "type": "object",
        "properties": {
            "direction": {
                "type": "string",
                "enum": ["download", "upload", "both"],
                "description": "Which direction(s) to measure. Defaults to both.",
            },
        },
        "required": [],
    }

    def __init__(self, client: SpeedTestClient) -> None:
        self._client = client

    def run(self, direction: str = "both", **kwargs: Any) -> Any:
        return self._client.run(direction=direction)
