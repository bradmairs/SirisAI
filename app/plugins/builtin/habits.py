"""Habit-suggestion tools (app/anticipation/habits.py)."""

from typing import TYPE_CHECKING, Any

from app.anticipation import habits
from app.plugins.base import Tool, ToolPermission
from app.skills.base import Skill

if TYPE_CHECKING:
    from app.core.siris import SirisAI


class HabitSuggestionsSkill(Skill):
    name = "habit_suggestions"
    description = (
        "Look for things the user does by hand at about the same time most days (lights, switches, media, covers) "
        "and list them as automations SirisAI could set up. Returns new and still-open suggestions."
    )
    parameters = {"type": "object", "properties": {}, "required": []}

    def run(self, siris: "SirisAI", **kwargs: Any) -> Any:
        fresh = habits.refresh(siris)
        return {"new": [s["id"] for s in fresh], "open": habits.suggestions(siris)}


class AcceptHabitSuggestionSkill(Skill):
    name = "accept_habit_suggestion"
    description = "Create the Home Assistant automation for a habit suggestion (id from habit_suggestions)."
    parameters = {"type": "object", "properties": {"suggestion_id": {"type": "string"}}, "required": ["suggestion_id"]}

    def run(self, siris: "SirisAI", suggestion_id: str = "", **kwargs: Any) -> Any:
        suggestion = next((s for s in habits.suggestions(siris, status=None) if s["id"] == suggestion_id), None)
        if suggestion is None:
            raise ValueError(f"Unknown habit suggestion: {suggestion_id}")
        if suggestion["status"] == "accepted":
            return {"suggestion_id": suggestion_id, "already_accepted": True}
        created = siris.run_tool(
            "home_assistant_create_automation",
            suggestion["automation"],
            # Accepting this exact suggestion (itself CONFIRM_REQUIRED) is the approval.
            confirmed=True,
            trigger=f"accept_habit_suggestion: {suggestion['description']}",
        )
        habits.set_status(siris, suggestion_id, "accepted", automation_id=created.get("automation_id"))
        return {"suggestion_id": suggestion_id, "automation": created}

    def dry_run(self, siris: "SirisAI", suggestion_id: str = "", **kwargs: Any) -> Any:
        suggestion = next((s for s in habits.suggestions(siris, status=None) if s["id"] == suggestion_id), None)
        return {"suggestion_id": suggestion_id, "would_create": suggestion["automation"] if suggestion else None}


class DismissHabitSuggestionTool(Tool):
    name = "dismiss_habit_suggestion"
    description = "Dismiss a habit suggestion so it isn't suggested again."
    permission = ToolPermission.SAFE_ACTION
    parameters = {"type": "object", "properties": {"suggestion_id": {"type": "string"}}, "required": ["suggestion_id"]}

    def __init__(self, siris: "SirisAI") -> None:
        self._siris = siris

    def run(self, suggestion_id: str = "", **kwargs: Any) -> Any:
        return habits.set_status(self._siris, suggestion_id, "dismissed")
