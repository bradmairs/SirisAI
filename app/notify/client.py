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
"""

from typing import Any

import httpx


class NotifyClient:
    def __init__(
        self, base_url: str, topic: str, timeout: float = 10.0, transport: httpx.BaseTransport | None = None
    ) -> None:
        self._topic = topic
        self._client = httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout, transport=transport)

    def send(self, message: str, title: str | None = None, actions: list[dict[str, Any]] | None = None) -> None:
        payload: dict[str, Any] = {"topic": self._topic, "message": message}
        if title:
            payload["title"] = title
        if actions:
            payload["actions"] = actions
        response = self._client.post("/", json=payload)
        response.raise_for_status()

    def close(self) -> None:
        self._client.close()
