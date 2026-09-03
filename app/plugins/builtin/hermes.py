"""Delegates homelab tasks to Hermes Agent (app/hermes/client.py) -- a
separate agent already running on this host with its own memories,
skills, and terminal access. SirisAI hands it a natural-language
instruction (Docker container control, other system-level operations)
rather than reimplementing what Hermes already knows how to do as its own
tools -- one delegate tool, not a growing set of Hermes-specific ones,
since the actual capability lives on Hermes's side.

SAFE_ACTION rather than CONFIRM_REQUIRED or RESTRICTED: mirrors how other
routine homelab operations (e.g. DockerRestartContainerTool) are already
gated here -- it runs immediately without a confirmation round-trip, but
every call is still fully logged to the audit trail like any other tool.
"""

from typing import Any

from app.hermes.client import HermesClient
from app.plugins.base import Tool, ToolPermission


class HermesRunTaskTool(Tool):
    name = "hermes_run_task"
    description = (
        "Delegate a homelab task to Hermes Agent -- a separate agent running on "
        "this host with its own memories, skills, and terminal access. Use this "
        "for Docker container control and other system-level operations Hermes "
        "already knows how to do (restarting a container, checking a service's "
        "status, running a command) instead of attempting it directly. Give a "
        "clear, specific natural-language instruction; Hermes reports back what "
        "it did."
    )
    permission = ToolPermission.SAFE_ACTION
    parameters = {
        "type": "object",
        "properties": {
            "instruction": {
                "type": "string",
                "description": "The task for Hermes Agent to carry out, in plain language.",
            }
        },
        "required": ["instruction"],
    }

    def __init__(self, client: HermesClient) -> None:
        self._client = client

    def run(self, instruction: str = "", **kwargs: Any) -> Any:
        return {"result": self._client.run_task(instruction)}
