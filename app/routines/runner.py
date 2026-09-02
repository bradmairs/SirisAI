"""Actually executing a routine (milestone 29) -- separate from
RoutineStore (app/routines/service.py), which only knows what's stored and
what's due. RoutineRunner is what POST /siris/routines/run_due calls: for
each due routine, run its actions in order through SirisAI.run_tool() (so
permission gating, audit logging, and CONFIRM_REQUIRED handling all apply
exactly as they do everywhere else), then mark it run regardless of
whether every action succeeded -- one broken step in a nightly check
shouldn't mean it's retried every minute until fixed, and the per-action
error is still visible in the result.
"""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.core.siris import SirisAI


class RoutineRunner:
    def __init__(self, siris: "SirisAI") -> None:
        self._siris = siris

    def run_due(self) -> list[dict[str, Any]]:
        return [self._run_one(routine) for routine in self._siris.routines.due_routines()]

    def _run_one(self, routine: dict[str, Any]) -> dict[str, Any]:
        trigger = f"routine: '{routine['name']}' ({routine['trigger_description']})"
        action_results = []
        for tool_name in routine["actions"]:
            try:
                output = self._siris.run_tool(tool_name, {}, trigger=trigger)
            except Exception as exc:
                action_results.append({"tool": tool_name, "result": None, "error": str(exc)})
            else:
                action_results.append({"tool": tool_name, "result": output, "error": None})

        self._siris.routines.mark_run(routine["id"])
        return {"routine_id": routine["id"], "name": routine["name"], "actions": action_results}
