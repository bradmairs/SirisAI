"""TTS speed (SIRISAI_TTS_SPEED) and the optional ffmpeg effect chain
(SIRISAI_TTS_EFFECT, app/voice/effects.py)."""

import asyncio
import json
import shutil
import subprocess

import httpx
import pytest

from app.core.config import Settings
from app.voice.base import TextToSpeechClient
from app.voice.effects import EffectTTSClient, apply_audio_filter
from app.voice.factory import build_tts_client
from app.voice.openai_compatible import OpenAICompatibleTTSClient


def _capturing_tts(speed: float) -> tuple[OpenAICompatibleTTSClient, list[dict]]:
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, content=b"mp3-bytes")

    client = OpenAICompatibleTTSClient(base_url="http://tts", model="kokoro", voice="bm_george", speed=speed)
    client._client = httpx.AsyncClient(base_url="http://tts", transport=httpx.MockTransport(handler))
    return client, bodies


def test_speed_is_sent_when_changed() -> None:
    client, bodies = _capturing_tts(1.1)
    assert asyncio.run(client.synthesize("hello")) == b"mp3-bytes"
    assert bodies[0]["speed"] == 1.1


def test_default_speed_leaves_the_request_unchanged() -> None:
    client, bodies = _capturing_tts(1.0)
    asyncio.run(client.synthesize("hello"))
    assert "speed" not in bodies[0]


def test_factory_wraps_only_when_an_effect_is_set() -> None:
    plain = build_tts_client(Settings(tts_provider="openai_compatible"))
    assert isinstance(plain, OpenAICompatibleTTSClient)

    wrapped = build_tts_client(Settings(tts_provider="openai_compatible", tts_effect="volume=3dB", tts_speed=1.1))
    assert isinstance(wrapped, EffectTTSClient)
    assert wrapped.audio_filter == "volume=3dB"
    assert isinstance(wrapped.inner, OpenAICompatibleTTSClient)
    assert wrapped.inner.speed == 1.1


class _FakeTTS(TextToSpeechClient):
    async def synthesize(self, text: str) -> bytes:
        return b"dry-audio"


def test_a_failing_effect_falls_back_to_the_unprocessed_audio() -> None:
    client = EffectTTSClient(_FakeTTS(), "volume=3dB", ffmpeg="/nonexistent/ffmpeg")
    assert asyncio.run(client.synthesize("hello")) == b"dry-audio"


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_the_filter_really_runs_through_ffmpeg() -> None:
    tone = subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=0.5",
         "-c:a", "libmp3lame", "-f", "mp3", "pipe:1"],
        capture_output=True, check=True,
    ).stdout

    processed = asyncio.run(apply_audio_filter(tone, "aecho=0.8:0.8:6:0.4,volume=-6dB"))

    assert processed and processed != tone


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_an_invalid_filter_raises_rather_than_returning_garbage() -> None:
    tone = subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=0.2",
         "-c:a", "libmp3lame", "-f", "mp3", "pipe:1"],
        capture_output=True, check=True,
    ).stdout

    with pytest.raises(RuntimeError):
        asyncio.run(apply_audio_filter(tone, "not_a_real_filter=1"))
