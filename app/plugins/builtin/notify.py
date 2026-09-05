"""Notify tools -- send a push notification via ntfy.sh
(app/notify/client.py), and read back queued non-urgent ones
(app/notify/pending.py, milestone 72). `NotifyTool` registers only when
SIRISAI_NTFY_TOPIC is set; `ListPendingNotificationsTool` is always
registered -- pending items can exist and be worth reading regardless of
whether a push channel is configured at all.
"""

from typing import Any

from app.notify.client import NotifyClient
from app.notify.pending import PendingNotificationStore
from app.plugins.base import Tool, ToolPermission


class NotifyTool(Tool):
    name = "notify"
    description = (
        "Send a push notification to the user's phone/desktop. Use sparingly -- only for "
        "things worth interrupting them for, not routine confirmations."
    )
    permission = ToolPermission.SAFE_ACTION
    # `actions` (ntfy action buttons, milestone 43) is deliberately not a
    # declared parameter here -- constructing one means building an exact
    # ntfy action-button payload (a URL, method, headers, body), which is
    # a programmatic concern for a caller that already knows the target
    # endpoint (see RoutineRunner._notify_pending_confirmation()), not
    # something worth prompting the model to assemble itself. run() still
    # accepts it as a plain kwarg for that internal use.
    parameters = {
        "type": "object",
        "properties": {
            "message": {"type": "string", "description": "The notification body."},
            "title": {"type": "string", "description": "Optional notification title."},
            "priority": {
                "type": "string",
                "enum": ["min", "low", "default", "high", "urgent"],
                "description": (
                    "Optional urgency level -- defaults to normal. Use 'urgent' sparingly, only for "
                    "things that genuinely need immediate attention."
                ),
            },
        },
        "required": ["message"],
    }

    def __init__(self, client: NotifyClient) -> None:
        self._client = client

    def run(
        self,
        message: str = "",
        title: str | None = None,
        priority: str | None = None,
        actions: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> Any:
        self._client.send(message, title=title, actions=actions, priority=priority)
        return {"sent": True}


class ListPendingNotificationsTool(Tool):
    name = "list_pending_notifications"
    description = (
        "List non-urgent notifications queued but not yet pushed -- warnings that were "
        "worth noting but not worth interrupting the user for right away. Normally these get "
        "folded into the next daily_briefing and cleared; this lets you check what's queued "
        "without waiting for that."
    )
    permission = ToolPermission.READ
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, pending: PendingNotificationStore) -> None:
        self._pending = pending

    def run(self, **kwargs: Any) -> Any:
        return self._pending.list_pending()
