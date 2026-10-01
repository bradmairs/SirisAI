"""Thin HTTP client for the SirisAI endpoints the satellite needs. The main
one is POST /siris/voice/converse -- a whole spoken turn, streamed back a
sentence at a time. transcribe/chat/speak remain for older servers.
"""

import json
from collections.abc import Iterator
from typing import Any

import httpx


class ConverseUnsupported(Exception):
    """The server predates /siris/voice/converse."""


class SirisClient:
    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        room: str = "",
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
        user: str = "",
    ) -> None:
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        if user:
            headers["X-Siris-User"] = user
        self._client = httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout, headers=headers, transport=transport)
        self._conversation_id: str | None = None
        self._room = room

    def converse(self, audio: bytes) -> Iterator[dict[str, Any]]:
        """Stream one spoken turn's events (see the server's
        POST /siris/voice/converse docstring for their shapes). Remembers
        the conversation id so the next turn continues the same
        conversation."""
        data: dict[str, str] = {}
        if self._conversation_id:
            data["conversation_id"] = self._conversation_id
        if self._room:
            data["room"] = self._room
        # A slow model can take a while before its first sentence; the read
        # timeout is the gap allowed between streamed lines, not the total.
        timeout = httpx.Timeout(30.0, read=120.0)
        with self._client.stream(
            "POST",
            "/siris/voice/converse",
            data=data,
            files={"file": ("recording.wav", audio, "audio/wav")},
            timeout=timeout,
        ) as response:
            if response.status_code in (404, 405):
                raise ConverseUnsupported()
            response.raise_for_status()
            for line in response.iter_lines():
                if not line.strip():
                    continue
                event = json.loads(line)
                if event.get("type") == "final" and event.get("conversation_id"):
                    self._conversation_id = event["conversation_id"]
                yield event

    def transcribe(self, audio: bytes) -> str:
        response = self._client.post(
            "/siris/voice/transcribe", files={"file": ("recording.wav", audio, "audio/wav")}
        )
        response.raise_for_status()
        return response.json()["text"]

    def chat(self, prompt: str) -> str:
        response = self._client.post(
            "/siris/chat",
            json={"prompt": prompt, "conversation_id": self._conversation_id, "room": self._room or None},
        )
        response.raise_for_status()
        body = response.json()
        self._conversation_id = body["conversation_id"]
        if body.get("confirmation_required"):
            call = body["confirmation_required"]
            return f"{call['name']} needs confirmation -- ask me from the web UI to confirm it."
        if body.get("tool_call"):
            return f"Done: {body['tool_call']['name']}."
        return body.get("response") or "I don't have a response for that."

    def register(self, room: str, callback_url: str) -> None:
        """Tell SirisAI which room this satellite is in and where to reach
        it (milestone 48) -- called once at startup when both are
        configured (see __main__.py). Not retried/heartbeated here;
        SatelliteStore.current_room() has no staleness cutoff, so a
        single registration is enough for this satellite to stay
        "current" until it (or another satellite) is heard from again."""
        response = self._client.post("/siris/satellites/register", json={"room": room, "callback_url": callback_url})
        response.raise_for_status()

    def speak(self, text: str) -> bytes:
        response = self._client.post("/siris/voice/speak", json={"text": text})
        response.raise_for_status()
        return response.content

    def close(self) -> None:
        self._client.close()
