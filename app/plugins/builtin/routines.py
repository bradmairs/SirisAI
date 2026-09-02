"""Routine tools -- how the LLM creates and manages routines (milestone
29). Always registered, same as goals: routines are core state, not an
optional integration.

Three tools, same single-purpose convention as the rest of
app/plugins/builtin/: create_routine, list_routines, set_routine_enabled.
No delete tool -- same as goals, a routine's history (when it last ran)
stays queryable rather than disappearing; disable it instead.
"""

from typing import Any

from app.plugins.base import Tool, ToolPermission, ToolRegistry
from app.routines.service import RoutineStore


class CreateRoutineTool(Tool):
    name = "create_routine"
    description = (
        "Create a named routine: an ordered list of existing tools to run together on a "
        "recurring basis, e.g. name='overnight_server_watch', trigger_description='nightly', "
        "interval_minutes=1440, actions=['server_health_check']. Does NOT schedule itself -- "
        "something external has to call POST /siris/routines/run_due for due routines to "
        "actually run, same as server_health_check itself needs an external trigger. Each "
        "action must be the name of an already-registered tool (see list_tools) -- a routine "
        "doesn't invent new capability, it composes existing ones."
    )
    permission = ToolPermission.SAFE_ACTION
    parameters = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "A short unique-ish label, e.g. 'overnight_server_watch'."},
            "trigger_description": {
                "type": "string",
                "description": "Free-text description of when this should run, e.g. 'nightly'.",
            },
            "interval_minutes": {
                "type": "integer",
                "description": "Run at least this often, in minutes, once due -- e.g. 1440 for nightly.",
            },
            "actions": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Names of existing registered tools to run in order, e.g. ['server_health_check'].",
            },
            "output_description": {
                "type": "string",
                "description": "Optional free-text note of what this routine's results are meant for.",
            },
        },
        "required": ["name", "trigger_description", "interval_minutes", "actions"],
    }

    def __init__(self, routines: RoutineStore, tools: ToolRegistry) -> None:
        self._routines = routines
        self._tools = tools

    def run(
        self,
        name: str = "",
        trigger_description: str = "",
        interval_minutes: int = 0,
        actions: list[str] | None = None,
        output_description: str | None = None,
        **kwargs: Any,
    ) -> Any:
        actions = actions or []
        unknown = [action for action in actions if self._tools.get(action) is None]
        if unknown:
            raise ValueError(f"Unknown tool name(s) in actions: {', '.join(unknown)}")
        if interval_minutes <= 0:
            raise ValueError("interval_minutes must be positive.")
        return self._routines.create_routine(
            name, trigger_description, interval_minutes, actions, output_description=output_description
        )


class ListRoutinesTool(Tool):
    name = "list_routines"
    description = "List routines, most recently created first, including when each last ran."
    permission = ToolPermission.READ
    parameters = {
        "type": "object",
        "properties": {
            "enabled_only": {"type": "boolean", "description": "If true, only list enabled routines. Defaults to false."},
        },
        "required": [],
    }

    def __init__(self, routines: RoutineStore) -> None:
        self._routines = routines

    def run(self, enabled_only: bool = False, **kwargs: Any) -> Any:
        return self._routines.list_routines(enabled_only=enabled_only)


class SetRoutineEnabledTool(Tool):
    name = "set_routine_enabled"
    description = "Enable or disable a routine by id -- a disabled routine is never due, but stays stored."
    permission = ToolPermission.SAFE_ACTION
    parameters = {
        "type": "object",
        "properties": {
            "routine_id": {"type": "integer", "description": "The routine to update."},
            "enabled": {"type": "boolean", "description": "New enabled state."},
        },
        "required": ["routine_id", "enabled"],
    }

    def __init__(self, routines: RoutineStore) -> None:
        self._routines = routines

    def run(self, routine_id: int = 0, enabled: bool = True, **kwargs: Any) -> Any:
        result = self._routines.set_enabled(routine_id, enabled)
        if result is None:
            raise ValueError(f"Unknown routine_id: {routine_id}")
        return result
