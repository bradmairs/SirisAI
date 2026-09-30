"""Confirming an action by voice: when a spoken turn stops for confirmation,
SirisAI asks out loud and the next thing said in that conversation ("yes",
"go ahead", "no", "cancel") answers it -- no phone needed.

In-memory and short-lived on purpose: a pending question only makes sense
for the next minute or so of the same conversation, and nothing about it
needs to survive a restart (the web UI's Confirm button still works either
way). Some domains are never confirmable by voice -- unlocking a door or
opening the garage stays a deliberate tap in the app, whoever happens to be
standing near a satellite (SIRISAI_VOICE_CONFIRMATION_BLOCKED_DOMAINS).
"""

import re
import time
from dataclasses import dataclass, field
from typing import Any

_PENDING_TTL_SECONDS = 120.0

_YES_RE = re.compile(
    r"^\s*(yes|yeah|yep|yup|sure|ok|okay|confirm(ed)?|do it|go ahead|please do|affirmative|"
    r"that's right|correct|proceed)\b",
    re.IGNORECASE,
)
_NO_RE = re.compile(
    r"^\s*(no|nope|nah|cancel|stop|don't|do not|never mind|nevermind|abort|negative|forget it)\b",
    re.IGNORECASE,
)


def classify_reply(text: str) -> str | None:
    """"yes", "no", or None when the reply is neither (a new request). Only
    short replies count -- "stop the music in the kitchen" is a new request,
    not a "no"."""
    if len(text.split()) > 6:
        return None
    if _NO_RE.search(text):
        return "no"
    if _YES_RE.search(text):
        return "yes"
    return None


def describe_call(tool_name: str, arguments: dict[str, Any]) -> str:
    """A short spoken description of a pending tool call."""
    if tool_name == "home_assistant_call_service":
        service = str(arguments.get("service", "")).replace("_", " ")
        target = arguments.get("entity_id") or arguments.get("domain") or ""
        target = str(target).split(".", 1)[-1].replace("_", " ")
        return f"{service} {target}".strip()
    words = tool_name.replace("_", " ")
    for key in ("name", "container", "title", "entity_id", "protocol"):
        if arguments.get(key):
            return f"{words}: {str(arguments[key]).replace('_', ' ')}"
    return words


def voice_confirmable(tool_name: str, arguments: dict[str, Any], blocked_domains: set[str]) -> bool:
    if tool_name == "home_assistant_call_service":
        domain = str(arguments.get("domain", "")).lower()
        entity = str(arguments.get("entity_id") or "").lower()
        entity_domain = entity.split(".", 1)[0] if "." in entity else ""
        return domain not in blocked_domains and entity_domain not in blocked_domains
    return tool_name not in blocked_domains


@dataclass
class PendingVoiceConfirmation:
    tool_name: str
    arguments: dict[str, Any]
    used_planner: bool
    expires_at: float = field(default_factory=lambda: time.monotonic() + _PENDING_TTL_SECONDS)


class PendingVoiceConfirmations:
    def __init__(self) -> None:
        self._pending: dict[str, PendingVoiceConfirmation] = {}

    def put(self, conversation_id: str, pending: PendingVoiceConfirmation) -> None:
        self._pending[conversation_id] = pending

    def pop(self, conversation_id: str) -> PendingVoiceConfirmation | None:
        pending = self._pending.pop(conversation_id, None)
        if pending is None or pending.expires_at < time.monotonic():
            return None
        return pending


pending_voice_confirmations = PendingVoiceConfirmations()
