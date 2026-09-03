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
external cron hits run_due, not a human watching a chat turn). Now, if a
notify channel and SIRISAI_PUBLIC_BASE_URL are both configured, that gets
a push notification with a one-tap "Confirm" action button -- ntfy's
`http` action type (https://ntfy.sh/docs/publish/#send-a-request-when-action-is-selected)
posting straight to POST /siris/tools/{tool_name}/run with
`confirmed: true`, not POST /siris/chat/confirm: a routine-originated
confirmation has no conversation_id (routines aren't chat turns) to hand
that endpoint, and /siris/tools/{name}/run is already the
conversation-independent way to run a tool directly -- exactly what a
notification tap, with no chat session behind it, actually is.
"""

import json
from typing import TYPE_CHECKING, Any

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
                self._notify_pending_confirmation(routine["name"], tool_name, exc.arguments)
            except Exception as exc:
                action_results.append({"tool": tool_name, "result": None, "error": str(exc)})
            else:
                action_results.append({"tool": tool_name, "result": output, "error": None})

        self._siris.routines.mark_run(routine["id"])
        return {"routine_id": routine["id"], "name": routine["name"], "actions": action_results}

    def _notify_pending_confirmation(self, routine_name: str, tool_name: str, arguments: dict[str, Any]) -> None:
        if self._siris.tools.get("notify") is None:
            return

        actions = None
        if self._siris.settings.public_base_url:
            headers = {"Content-Type": "application/json"}
            if self._siris.settings.api_key:
                headers["Authorization"] = f"Bearer {self._siris.settings.api_key}"
            actions = [
                {
                    "action": "http",
                    "label": "Confirm",
                    "url": f"{self._siris.settings.public_base_url.rstrip('/')}/siris/tools/{tool_name}/run",
                    "method": "POST",
                    "headers": headers,
                    "body": json.dumps({"arguments": arguments, "confirmed": True}),
                }
            ]

        message = f"Routine '{routine_name}' wants to run '{tool_name}' ({arguments}) -- needs your confirmation."
        self._siris.run_tool(
            "notify",
            {"title": "SirisAI: confirmation needed", "message": message, "actions": actions},
            trigger=f"routine: '{routine_name}' action needed confirmation",
            evidence={"tool_name": tool_name, "arguments": arguments},
        )
