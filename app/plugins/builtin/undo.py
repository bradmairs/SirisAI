"""Undo by saying so (app/core/undo.py, 5.0)."""

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
    parameters = {
        "type": "object",
        "properties": {"which": {"type": "string", "description": "6.48, optional: not the last one but this: its number from "
                                 "undo_history (1 = the last), 'the lights', or '10 minutes ago'."}},
        "required": [],
    }

    def __init__(self, siris: "SirisAI") -> None:
        self._siris = siris

    def needs_confirmation(self, arguments: dict[str, Any]) -> bool:
        from app.core import undo

        which = arguments.get("which")
        if which:
            entry = undo.find(self._siris, which)
            return entry is not None and undo.needs_confirming(self._siris, entry={k: v for k, v in entry.items() if k not in ("number", "minutes_ago")})
        return undo.needs_confirming(self._siris)

    def dry_run(self, **kwargs: Any) -> Any:
        from app.core import undo

        entry = undo.last(self._siris)
        return {"would_undo": entry["label"] if entry else None}

    def run(self, **kwargs: Any) -> Any:
        from app.core import undo

        # Only reached once confirmed when a reversing call needs it.
        which = kwargs.get("which")
        return undo.undo_last(self._siris, confirmed=self.needs_confirmation({"which": which}), which=which)


class UndoHistoryTool(Tool):
    """6.48: what can still be undone, not just the last thing."""

    name = "undo_history"
    description = ("What SirisAI did for this person in the last two hours that can still be undone, newest first and numbered. "
                   "Then undo_last with which= a number, 'the lights' or '10 minutes ago'. For 'what can you undo'.")
    permission = ToolPermission.READ
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, siris: "SirisAI") -> None:
        self._siris = siris

    def run(self, **kwargs: Any) -> Any:
        from app.core import undo

        entries = undo.history(self._siris)
        if not entries:
            return {"entries": [], "text": "Nothing from the last two hours can be undone."}
        text = "; ".join(f"{e['number']}. {e['label']} ({e['minutes_ago']} min ago)" for e in entries[:10])
        return {"entries": [{"number": e["number"], "label": e["label"], "minutes_ago": e["minutes_ago"]} for e in entries],
                "text": f"I can undo: {text}."}
