"""Thin client for ntfy.sh (https://ntfy.sh/docs/publish/) -- push
notifications, no account or API key needed, just a topic name. Point
`base_url` at a self-hosted ntfy instance to keep this fully local, or
use the public ntfy.sh service.

Uses ntfy's JSON publish endpoint (POST to the base URL with a JSON body
carrying "topic") rather than the simpler header-based one -- a fully
equivalent, equally well-supported publish mode, needed here because it's
the only one that can carry `actions` (milestone 43, actionable push
notifications): https://ntfy.sh/docs/publish/#action-buttons. A plain
title-only message still round-trips through this the same way it always
did, just as a JSON body instead of a header.

`priority` (escalation tiers, milestone 44) is ntfy's own vocabulary --
"min"/"low"/"default"/"high"/"urgent" (or the 1-5 equivalent, both are
accepted: https://ntfy.sh/docs/publish/#message-priority) -- passed
through as-is rather than validated here; an invalid value fails loudly
against the real ntfy server rather than silently, same "let a mismatch
surface rather than hiding it" posture as this codebase's other
thin-client integrations.
"""

import json
from typing import Any

import httpx


_NTFY_PRIORITIES = {"min": 1, "low": 2, "default": 3, "normal": 3, "high": 4, "urgent": 5, "max": 5}


def _ntfy_priority(priority: str | int | None) -> int | None:
    """ntfy's JSON publish API takes priority as an integer 1-5 and answers a
    400 to a name like "default" (found live, 2026-09-26: a reminder failed
    because the model passed priority="default"). Accept names or numbers."""
    if priority is None or priority == "":
        return None
    if isinstance(priority, int):
        return priority if 1 <= priority <= 5 else None
    text = str(priority).strip().lower()
    if text.isdigit():
        return int(text) if 1 <= int(text) <= 5 else None
    return _NTFY_PRIORITIES.get(text)


# ntfy turns a message over 4,096 bytes into an attachment, which ntfy.sh refuses
# for JSON publishes (413, sometimes 500): every daily briefing in early
# November 2026 was 5,600-16,000 characters and never arrived. Keep well under.
MAX_MESSAGE_BYTES = 3800
_MORE = " … (more in SirisOS)"


def fit_message(message: str, limit: int = MAX_MESSAGE_BYTES) -> str:
    """The message, cut at a word boundary to fit ntfy's limit in UTF-8 bytes."""
    if len(message.encode("utf-8")) <= limit:
        return message
    room = limit - len(_MORE.encode("utf-8"))
    cut = message.encode("utf-8")[:room].decode("utf-8", errors="ignore")
    space = cut.rfind(" ")
    if space > room * 0.8:
        cut = cut[:space]
    return cut.rstrip(" ,;:") + _MORE


# 6.73: ntfy limits the whole JSON body to 4,096 bytes, not just the message
# (its "JSON body too large" 413), so a long title, action buttons or escaped
# characters could still push a fitted message over. Measure the body itself.
MAX_BODY_BYTES = 4000


def _encode(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _fit_body(payload: dict[str, Any], message: str) -> bytes:
    """The JSON body, with the message shortened until the body fits ntfy."""
    if "title" in payload:
        payload["title"] = fit_message(payload["title"], 250).replace(_MORE, "…")
    body = _encode(payload)
    while len(body) > MAX_BODY_BYTES:
        room = len(payload["message"].encode("utf-8")) - (len(body) - MAX_BODY_BYTES) - 50
        if room < 200:
            payload.pop("actions", None)
            room = max(200, room)
        payload["message"] = fit_message(message, room)
        body = _encode(payload)
        if room <= 200 and len(body) > MAX_BODY_BYTES:
            break
    return body


class NotifyClient:
    def __init__(
        self, base_url: str, topic: str, timeout: float = 10.0, transport: httpx.BaseTransport | None = None,
        click_url: str = "", token: str = "",
    ) -> None:
        self._topic = topic
        self._click_url = click_url
        # 6.68: an ntfy access token (SIRISAI_NTFY_TOKEN) for a topic that's reserved or on a server with auth.
        headers = {"Authorization": f"Bearer {token}"} if token else None
        self._client = httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout, transport=transport, headers=headers)

    def send(
        self,
        message: str,
        title: str | None = None,
        actions: list[dict[str, Any]] | None = None,
        priority: str | None = None,
    ) -> None:
        payload: dict[str, Any] = {"topic": self._topic, "message": fit_message(message)}
        if self._click_url and payload["message"] != message:
            payload["click"] = self._click_url   # tapping a shortened push opens SirisOS
        if title:
            payload["title"] = title
        if actions:
            payload["actions"] = actions
        level = _ntfy_priority(priority)
        if level is not None:
            payload["priority"] = level
        body = _fit_body(payload, message)
        if self._click_url and payload["message"] != message:
            payload["click"] = self._click_url
            body = _fit_body(payload, message)
        response = self._client.post("/", content=body, headers={"Content-Type": "application/json"})
        response.raise_for_status()

    def close(self) -> None:
        self._client.close()
