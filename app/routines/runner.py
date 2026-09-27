"""Actually executing a routine (milestone 29) -- separate from
RoutineStore (app/routines/service.py), which only knows what's stored and
what's due. RoutineRunner is what POST /siris/routines/run_due calls: for
each due routine, run its actions in order through SirisAI.run_tool() (so
permission gating, audit logging, and CONFIRM_REQUIRED handling all apply
exactly as they do everywhere else), then mark it run regardless of
whether every action succeeded -- one broken step in a nightly check
shouldn't mean it's retried every minute until fixed, and the per-action
error is still visible in the result.

Actionable push notifications (milestone 43): a routine's action hitting
a CONFIRM_REQUIRED tool used to just be recorded as an error and dropped
-- nothing else ever surfaced it, since a routine runs unattended (an
external cron hits run_due, not a human watching a chat turn). Now that's
`notify_confirmation_needed` (app/notify/confirmation.py) -- shared with
Cortex, which hits the same gap for an ordinary chat turn (the user may
not be looking at the app when a tool call needs confirming); see that
module's own docstring for the notification shape.
"""

from typing import TYPE_CHECKING, Any

from app.notify.confirmation import notify_confirmation_needed
from app.plugins.base import ConfirmationRequired

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
            except ConfirmationRequired as exc:
                action_results.append({"tool": tool_name, "result": None, "error": str(exc)})
                notify_confirmation_needed(self._siris, f"Routine '{routine['name']}'", tool_name, exc.arguments)
            except Exception as exc:
                action_results.append({"tool": tool_name, "result": None, "error": str(exc)})
            else:
                action_results.append({"tool": tool_name, "result": output, "error": None})

        self._siris.routines.mark_run(routine["id"])
        return {"routine_id": routine["id"], "name": routine["name"], "actions": action_results}
