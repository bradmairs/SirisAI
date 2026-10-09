"""Push a notification with a one-tap "Confirm" button when something needs
confirmation but nobody is necessarily watching for it -- shared by
RoutineRunner (a routine's action hits a CONFIRM_REQUIRED tool unattended)
and Cortex (a chat turn does the same, and the user may not be looking at
the app when it happens).

Always hits POST /siris/tools/{tool_name}/run directly with confirmed: true
-- not POST /siris/chat/confirm -- since a notification tap has no chat
session behind it; the conversation-independent generic endpoint is exactly
what it is. ntfy's `http` action type
(https://ntfy.sh/docs/publish/#send-a-request-when-action-is-selected)
posts to it directly from the notification, no app open required.

Best-effort and silent by design (same posture as everywhere else a
notification is optional infrastructure, not a dependency): no `notify`
tool registered, or no SIRISAI_PUBLIC_BASE_URL configured, just means the
action button is omitted -- see each caller for what it does when this
raises or is skipped.
"""

import json
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.core.siris import SirisAI

logger = logging.getLogger(__name__)


def notify_confirmation_needed(siris: "SirisAI", context: str, tool_name: str, arguments: dict[str, Any]) -> None:
    # The attention feed gets it whether or not a push channel exists, with
    # working Approve/Decline actions (app/hub/attention.py).
    try:
        from app.hub.attention import raise_confirmation

        raise_confirmation(siris, context, tool_name, arguments)
    except Exception:
        logger.exception("Adding a confirmation to the attention feed failed")

    if siris.tools.get("notify") is None:
        return

    actions = None
    if siris.settings.public_base_url:
        headers = {"Content-Type": "application/json"}
        if siris.settings.api_key:
            headers["Authorization"] = f"Bearer {siris.settings.api_key}"
        actions = [
            {
                "action": "http",
                "label": "Confirm",
                "url": f"{siris.settings.public_base_url.rstrip('/')}/siris/tools/{tool_name}/run",
                "method": "POST",
                "headers": headers,
                "body": json.dumps({"arguments": arguments, "confirmed": True}),
            }
        ]

    message = f"{context} wants to run '{tool_name}' ({arguments}) -- needs your confirmation."
    siris.run_tool(
        "notify",
        {"title": "SirisAI: confirmation needed", "message": message, "actions": actions},
        trigger=f"{context}: action needed confirmation",
        evidence={"tool_name": tool_name, "arguments": arguments},
    )
