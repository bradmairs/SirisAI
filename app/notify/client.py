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


class NotifyClient:
    def __init__(
        self, base_url: str, topic: str, timeout: float = 10.0, transport: httpx.BaseTransport | None = None
    ) -> None:
        self._topic = topic
        self._client = httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout, transport=transport)

    def send(
        self,
        message: str,
        title: str | None = None,
        actions: list[dict[str, Any]] | None = None,
        priority: str | None = None,
    ) -> None:
        payload: dict[str, Any] = {"topic": self._topic, "message": message}
        if title:
            payload["title"] = title
        if actions:
            payload["actions"] = actions
        level = _ntfy_priority(priority)
        if level is not None:
            payload["priority"] = level
        response = self._client.post("/", json=payload)
        response.raise_for_status()

    def close(self) -> None:
        self._client.close()
