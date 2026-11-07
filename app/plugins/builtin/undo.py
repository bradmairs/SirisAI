"""Undo by saying so (app/core/undo.py, 4.9)."""

from typing import TYPE_CHECKING, Any

from app.plugins.base import Tool, ToolPermission

if TYPE_CHECKING:
    from app.core.siris import SirisAI


class UndoLastTool(Tool):
    name = "undo_last"
    description = (
        "Undo the last thing SirisAI did for this person in the last two hours: a light or device it turned on or "
        "off, a thermostat it set, a scene or protocol, a reminder, an automation, a filed document or a setting. "
        "For 'undo that', 'take that back', 'put it back'. Never unlocks, opens or disarms anything."
    )
    # Asks first only when a reversing call would itself ask (deleting an
    # automation): turning a light back off doesn't need a second OK.
    permission = ToolPermission.CONFIRM_REQUIRED
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, siris: "SirisAI") -> None:
        self._siris = siris

    def needs_confirmation(self, arguments: dict[str, Any]) -> bool:
        from app.core import undo

        return undo.needs_confirming(self._siris)

    def dry_run(self, **kwargs: Any) -> Any:
        from app.core import undo

        entry = undo.last(self._siris)
        return {"would_undo": entry["label"] if entry else None}

    def run(self, **kwargs: Any) -> Any:
        from app.core import undo

        # Only reached once confirmed when a reversing call needs it.
        return undo.undo_last(self._siris, confirmed=undo.needs_confirming(self._siris))
