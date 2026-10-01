"""what_did_you_do (app/audit/autonomy.py)."""

from typing import TYPE_CHECKING, Any

from app.audit.autonomy import timeline
from app.plugins.base import Tool, ToolPermission

if TYPE_CHECKING:
    from app.core.siris import SirisAI


class WhatDidYouDoTool(Tool):
    name = "what_did_you_do"
    description = (
        "What SirisAI did on its own recently -- notifications sent, containers restarted, devices changed, alerts, "
        "energy jobs, things remembered -- with why each happened. For 'what have you done today?'."
    )
    permission = ToolPermission.READ
    parameters = {
        "type": "object",
        "properties": {"hours": {"type": "number", "description": "How far back to look. Defaults to 24."}},
        "required": [],
    }

    def __init__(self, siris: "SirisAI") -> None:
        self._siris = siris

    def run(self, hours: float = 24, **kwargs: Any) -> Any:
        return timeline(self._siris, hours=hours)
