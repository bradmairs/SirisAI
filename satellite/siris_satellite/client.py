"""Thin HTTP client for the SirisAI endpoints the satellite needs:
transcribe, chat, speak. These are the exact same endpoints the built-in
web UI uses (app/web/static/index.html) -- the satellite is just another
frontend, nothing on the SirisAI side changes for it to exist.
"""

import httpx


class SirisClient:
    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._client = httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout, headers=headers, transport=transport)
        self._conversation_id: str | None = None

    def transcribe(self, audio: bytes) -> str:
        response = self._client.post(
            "/siris/voice/transcribe", files={"file": ("recording.wav", audio, "audio/wav")}
        )
        response.raise_for_status()
        return response.json()["text"]

    def chat(self, prompt: str) -> str:
        response = self._client.post(
            "/siris/chat", json={"prompt": prompt, "conversation_id": self._conversation_id}
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

    def speak(self, text: str) -> bytes:
        response = self._client.post("/siris/voice/speak", json={"text": text})
        response.raise_for_status()
        return response.content

    def close(self) -> None:
        self._client.close()
