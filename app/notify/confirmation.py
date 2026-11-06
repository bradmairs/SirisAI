"""Push a notification with a one-tap "Confirm" button when something needs
confirmation but nobody is necessarily watching for it -- shared by
RoutineRunner (a routine's action hits a CONFIRM_REQUIRED tool unattended)
and Cortex (a chat turn does the same, and the user may not be looking at
the app when it happens).

The buttons are Approve and Decline, posting to the attention item's own
act endpoint (POST /siris/hub/v1/attention/{id}/act), so a tap on the phone
does exactly what the feed's buttons do: runs the held call with
confirmed: true (or declines it), closes the item everywhere, and is
counted by the learning loop. Without an attention feed (a stand-in
SirisAI) it falls back to a single Confirm posting to POST
/siris/tools/{tool_name}/run directly. ntfy's `http` action type
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

_DESCRIBED = {"ssh_power", "home_assistant_call_service", "home_assistant_create_automation", "run_protocol"}


def _headers(siris: "SirisAI") -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if siris.settings.api_key:
        headers["Authorization"] = f"Bearer {siris.settings.api_key}"
    return headers


def item_actions(siris: "SirisAI", item: dict[str, Any]) -> list[dict[str, Any]] | None:
    """ntfy buttons for an attention item's own actions (up to three), each
    posting to POST /siris/hub/v1/attention/{id}/act -- the same thing the
    feed's buttons do, so a tap on the phone is recorded, closes the item
    everywhere, and counts for the learning loop. None without
    SIRISAI_PUBLIC_BASE_URL (the phone has nowhere to send the tap)."""
    if not siris.settings.public_base_url:
        return None
    from app.core import approvals

    base = siris.settings.public_base_url.rstrip("/")
    payload = siris.attention._payload(item["id"])
    # A button can't carry the approval PIN: big actions get Decline only and
    # are approved in the app.
    actions = [a for a in item["actions"] if not approvals.action_needs_pin(siris, item, a["id"], payload)]
    buttons: list[dict[str, Any]] = []
    held = len(actions) < len(item["actions"])
    if held and siris.settings.sirisos_public_url:
        # The ones that need the PIN open straight to the approval in
        # SirisOS (4.6), which asks for it there.
        buttons.append({
            "action": "view",
            "label": "Approve in SirisOS",
            "url": f"{siris.settings.sirisos_public_url.rstrip('/')}/?attention={item['id']}",
            "clear": True,
        })
    return buttons + [
        {
            "action": "http",
            "label": a["label"],
            "url": f"{base}/siris/hub/v1/attention/{item['id']}/act",
            "method": "POST",
            "headers": {**_headers(siris), "X-Siris-Client": "your phone"},
            "body": json.dumps({"action": a["id"]}),
            "clear": True,
        }
        for a in actions[: 3 - len(buttons)]
    ]


def notify_confirmation_needed(siris: "SirisAI", context: str, tool_name: str, arguments: dict[str, Any]) -> None:
    # The attention feed gets it whether or not a push channel exists, with
    # working Approve/Decline actions (app/hub/attention.py).
    item = None
    try:
        from app.hub.attention import raise_confirmation

        item = raise_confirmation(siris, context, tool_name, arguments)
    except Exception:
        logger.exception("Adding a confirmation to the attention feed failed")

    if siris.tools.get("notify") is None:
        return

    from app.core import approvals

    if item is not None:
        # Approve / Decline on the phone act on the feed item itself.
        actions = item_actions(siris, item)
    elif siris.settings.public_base_url and not approvals.tool_needs_pin(siris, tool_name, arguments):
        actions = [
            {
                "action": "http",
                "label": "Confirm",
                "url": f"{siris.settings.public_base_url.rstrip('/')}/siris/tools/{tool_name}/run",
                "method": "POST",
                "headers": _headers(siris),
                "body": json.dumps({"arguments": arguments, "confirmed": True}),
            }
        ]
    else:
        actions = None

    # In words for the calls that have a plain description (4.6); the rest
    # show the call itself, which says more than the tool's name alone.
    if tool_name in _DESCRIBED:
        from app.voice.confirmations import describe_call

        message = f"{context} wants to {describe_call(tool_name, arguments)}. It needs your OK."
    else:
        message = f"{context} wants to run '{tool_name}' ({arguments}) -- needs your confirmation."
    siris.run_tool(
        "notify",
        {"title": "SirisAI: confirmation needed", "message": message, "actions": actions},
        trigger=f"{context}: action needed confirmation",
        evidence={"tool_name": tool_name, "arguments": arguments},
    )
