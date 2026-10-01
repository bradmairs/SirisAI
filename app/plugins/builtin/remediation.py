"""remediation_status: what self-healing (app/remediation/runner.py) is
watching, what it has done in the last hour, and how much of its budget is left."""

from typing import TYPE_CHECKING, Any

from app.plugins.base import Tool, ToolPermission
from app.remediation import runner

if TYPE_CHECKING:
    from app.core.siris import SirisAI


class RemediationStatusTool(Tool):
    name = "remediation_status"
    description = (
        "Self-healing status: which containers SirisAI restarts on its own when they go down, what it has fixed or "
        "escalated in the last hour, and how many automatic actions it has left this hour."
    )
    permission = ToolPermission.READ
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, siris: "SirisAI") -> None:
        self._siris = siris

    def run(self, **kwargs: Any) -> Any:
        return runner.status(self._siris)
