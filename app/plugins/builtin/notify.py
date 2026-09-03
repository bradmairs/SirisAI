"""Notify tool -- send a push notification via ntfy.sh
(app/notify/client.py). Registered only when SIRISAI_NTFY_TOPIC is set.
"""

from typing import Any

from app.notify.client import NotifyClient
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
        },
        "required": ["message"],
    }

    def __init__(self, client: NotifyClient) -> None:
        self._client = client

    def run(
        self, message: str = "", title: str | None = None, actions: list[dict[str, Any]] | None = None, **kwargs: Any
    ) -> Any:
        self._client.send(message, title=title, actions=actions)
        return {"sent": True}
