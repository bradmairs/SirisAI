"""Speech clients for any backend that speaks OpenAI's audio API shape.

That's a de facto standard several self-hostable local servers implement --
whisper.cpp's server example, speaches/faster-whisper-server for STT, and
Piper-based wrappers like openedai-speech for TTS -- so one client here
covers all of them; point `base_url` at whichever is running on your
homelab. A provider with a genuinely different wire format gets its own
client next to this one, picked in app/voice/factory.py.
"""

import httpx

from app.voice.base import SpeechToTextClient, TextToSpeechClient


class OpenAICompatibleSTTClient(SpeechToTextClient):
    def __init__(self, base_url: str, model: str, timeout: float = 60.0) -> None:
        self.model = model
        self._client = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout)

    async def transcribe(self, audio: bytes, *, filename: str = "audio.wav", content_type: str = "audio/wav") -> str:
        response = await self._client.post(
            "/v1/audio/transcriptions",
            data={"model": self.model},
            files={"file": (filename, audio, content_type)},
        )
        response.raise_for_status()
        return response.json()["text"]

    async def aclose(self) -> None:
        await self._client.aclose()


class OpenAICompatibleTTSClient(TextToSpeechClient):
    def __init__(self, base_url: str, model: str, voice: str, speed: float = 1.0, timeout: float = 60.0) -> None:
        self.model = model
        self.voice = voice
        self.speed = speed
        self._client = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout)

    async def synthesize(self, text: str) -> bytes:
        body: dict[str, object] = {"model": self.model, "voice": self.voice, "input": text, "response_format": "mp3"}
        # Only sent when changed, so backends see exactly the request they did before.
        if self.speed != 1.0:
            body["speed"] = self.speed
        response = await self._client.post("/v1/audio/speech", json=body)
        response.raise_for_status()
        return response.content

    async def aclose(self) -> None:
        await self._client.aclose()
