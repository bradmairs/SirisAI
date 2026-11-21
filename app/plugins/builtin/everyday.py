"""Everyday tools added in 6.58: the brief's preferences, reminders on a
device's state, the weekly review, and SirisAI's own health."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.plugins.base import Tool, ToolPermission

if TYPE_CHECKING:
    from app.core.siris import SirisAI


class _SirisTool(Tool):
    def __init__(self, siris: "SirisAI") -> None:
        self._siris = siris


class BriefingPreferencesTool(_SirisTool):
    name = "briefing_preferences"
    description = ("Change the morning brief: hide parts you don't want ('leave Plex out of my briefing'), show them again, or put "
                   "some first ('weather first'). Parts: weather, forecast, calendar, todo, reminders, plex, warnings, queued, goals, "
                   "health, training, email, car, parcels, birthdays, on_this_day. Parts the same as yesterday are folded into one line by themselves.")
    permission = ToolPermission.SAFE_ACTION
    parameters = {
        "type": "object",
        "properties": {
            "hide": {"type": "array", "items": {"type": "string"}},
            "show": {"type": "array", "items": {"type": "string"}},
            "first": {"type": "array", "items": {"type": "string"}, "description": "These first, in this order"},
        },
        "required": [],
    }

    def run(self, hide: list[str] | None = None, show: list[str] | None = None, first: list[str] | None = None, **kwargs: Any) -> Any:
        from app.skills.builtin import briefing_adapt

        return briefing_adapt.set_prefs(self._siris, hide, show, first)


class CreateStateReminderTool(_SirisTool):
    name = "create_state_reminder"
    description = ("A reminder for when a device changes: 'remind me to hang the washing out when the washing machine finishes'. "
                   "entity_id is the Home Assistant entity (find it with home_assistant_resolve_entity); to_state fires when it "
                   "changes to that state (e.g. 'off', 'idle', 'open'); below fires when a number drops to it after being above, "
                   "e.g. a smart plug's power below 5 W when a cycle ends. For 'when I get home' or 'near the shops' use "
                   "create_geofence_reminder instead.")
    permission = ToolPermission.SAFE_ACTION
    parameters = {
        "type": "object",
        "properties": {
            "entity_id": {"type": "string"},
            "message": {"type": "string", "description": "What to remind about"},
            "to_state": {"type": "string"},
            "below": {"type": "number"},
        },
        "required": ["entity_id", "message"],
    }

    def run(self, entity_id: str = "", message: str = "", to_state: str | None = None, below: float | None = None, **kwargs: Any) -> Any:
        from app.reminders import conditions

        return conditions.create(self._siris, entity_id, message, to_state, below)


class StateRemindersTool(_SirisTool):
    name = "state_reminders"
    description = "The reminders waiting on a device's state (create_state_reminder); cancel one with cancel_id."
    permission = ToolPermission.SAFE_ACTION
    parameters = {"type": "object", "properties": {"cancel_id": {"type": "integer"}}, "required": []}

    def run(self, cancel_id: int | None = None, **kwargs: Any) -> Any:
        from app.reminders import conditions

        if cancel_id is not None:
            return conditions.cancel(self._siris, int(cancel_id))
        return {"waiting": conditions.listed(self._siris)}


class WeeklyReviewTool(_SirisTool):
    name = "weekly_review"
    description = ("This week in review: what got done (goals, and what SirisAI did by area), notes and decisions in Siris, "
                   "servers, training and sleep. Siris's weekly review note includes it; this shows it now.")
    permission = ToolPermission.READ
    parameters = {"type": "object", "properties": {}, "required": []}

    def run(self, **kwargs: Any) -> Any:
        from app.upkeep import weekly_review

        return weekly_review.compose(self._siris)


class SirisHealthTool(_SirisTool):
    name = "sirisai_health"
    description = ("How SirisAI itself is doing: reply times and whether they're getting slower, failing or paused tools, model "
                   "fallbacks, background jobs, and the self-test. For 'are you working properly', 'why are you slow'.")
    permission = ToolPermission.READ
    parameters = {"type": "object", "properties": {}, "required": []}

    def run(self, **kwargs: Any) -> Any:
        from app.upkeep import dashboard

        return dashboard.build(self._siris)


EVERYDAY_TOOLS = (BriefingPreferencesTool, CreateStateReminderTool, StateRemindersTool, WeeklyReviewTool, SirisHealthTool)


class WatchForTool(Tool):
    """7.37: "tell me when..." for anything a read-only tool can look up (app/reminders/watches.py)."""

    name = "watch_for"
    description = (
        "Tell the user when something becomes true, by re-checking a read-only tool every few minutes: 'tell me when "
        "Dune is on Plex' (tool plex_search, arguments {query: Dune}, text_contains 'dune'); 'when the download "
        "finishes' (sonarr_queue, text_missing the show's name); 'when the NAS has under 100 GB free' (a disk tool, "
        "number_at the field, below 100); 'when it stops raining' (weather_current, text_missing 'rain'). "
        "For a Home Assistant device changing state use create_state_reminder instead."
    )
    permission = ToolPermission.SAFE_ACTION
    parameters = {
        "type": "object",
        "properties": {
            "what": {"type": "string", "description": "What you'll tell them, e.g. 'Dune is on Plex'."},
            "tool": {"type": "string", "description": "A read-only tool to re-run."},
            "arguments": {"type": "object", "description": "That tool's arguments."},
            "text_contains": {"type": "string", "description": "Fire when the result contains this text."},
            "text_missing": {"type": "string", "description": "Fire when the result no longer contains this text."},
            "number_at": {"type": "string", "description": "Dotted path to a number in the result, e.g. '0.free_gb'."},
            "below": {"type": "number"},
            "above": {"type": "number"},
            "every_minutes": {"type": "integer", "description": "How often to check (at least 5). Defaults to 15."},
            "days": {"type": "integer", "description": "Give up after this many days. Defaults to 7."},
        },
        "required": ["what", "tool"],
    }

    def __init__(self, siris: Any) -> None:
        self._siris = siris

    def run(self, what: str = "", tool: str = "", arguments: dict | None = None, **kwargs: Any) -> Any:
        from app.reminders import watches

        allowed = {k: kwargs[k] for k in ("text_contains", "text_missing", "number_at", "below", "above", "every_minutes", "days") if kwargs.get(k) not in (None, "")}
        return watches.create(self._siris, what, tool, arguments or {}, **allowed)


class WatchesTool(Tool):
    name = "watches"
    description = "List the 'tell me when...' watches, or cancel one (cancel_id)."
    permission = ToolPermission.SAFE_ACTION
    parameters = {"type": "object", "properties": {"cancel_id": {"type": "integer"}}, "required": []}

    def __init__(self, siris: Any) -> None:
        self._siris = siris

    def run(self, cancel_id: int | None = None, **kwargs: Any) -> Any:
        from app.reminders import watches

        if cancel_id is not None:
            return {"cancelled": watches.cancel(self._siris, int(cancel_id))}
        return [{k: w[k] for k in ("id", "what", "tool", "every_minutes", "until")} for w in self._siris.state.get(watches.STATE_KEY) or []]


class SetupCheckTool(Tool):
    name = "setup_check"
    description = (
        "What parts of SirisAI aren't set up yet, what each would unlock, and settings that look wrong "
        "('what am I missing?', 'what else can you do if I set it up?', 'is anything misconfigured?')."
    )
    permission = ToolPermission.READ
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, siris: Any) -> None:
        self._siris = siris

    def run(self, **kwargs: Any) -> Any:
        from app.upkeep import setup_check

        found = setup_check.report(self._siris)
        return {"summary": found["summary"], "not_set_up": found["not_set_up"], "problems": found["problems"]}


EVERYDAY_TOOLS = (*EVERYDAY_TOOLS, WatchForTool, WatchesTool, SetupCheckTool)   # 7.37
