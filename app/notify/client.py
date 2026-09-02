"""Thin client for ntfy.sh (https://ntfy.sh/docs/) -- push notifications
over a plain HTTP POST, no account or API key needed, just a topic name.
Point `base_url` at a self-hosted ntfy instance to keep this fully local,
or use the public ntfy.sh service.
"""

import httpx


class NotifyClient:
    def __init__(
        self, base_url: str, topic: str, timeout: float = 10.0, transport: httpx.BaseTransport | None = None
    ) -> None:
        self._topic = topic
        self._client = httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout, transport=transport)

    def send(self, message: str, title: str | None = None) -> None:
        headers = {"Title": title} if title else {}
        response = self._client.post(f"/{self._topic}", content=message.encode(), headers=headers)
        response.raise_for_status()

    def close(self) -> None:
        self._client.close()
