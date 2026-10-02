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


def test_factory_applies_the_configured_effect(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.voice.factory.shutil.which", lambda name: "/usr/bin/ffmpeg")
    wrapped = build_tts_client(Settings(tts_provider="openai_compatible", tts_effect="volume=3dB", tts_speed=1.1))
    assert isinstance(wrapped, EffectTTSClient)
    assert wrapped.audio_filter == "volume=3dB"
    assert isinstance(wrapped.inner, OpenAICompatibleTTSClient)
    assert wrapped.inner.speed == 1.1


def test_factory_still_re_encodes_without_an_effect_when_ffmpeg_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    """So the HUD can join sentence clips -- see app/voice/effects.py."""
    monkeypatch.setattr("app.voice.factory.shutil.which", lambda name: "/usr/bin/ffmpeg")
    wrapped = build_tts_client(Settings(tts_provider="openai_compatible"))
    assert isinstance(wrapped, EffectTTSClient)
    assert wrapped.audio_filter == "anull"


def test_factory_leaves_the_client_alone_without_ffmpeg_or_an_effect(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.voice.factory.shutil.which", lambda name: None)
    assert isinstance(build_tts_client(Settings(tts_provider="openai_compatible")), OpenAICompatibleTTSClient)


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


def _decoded_seconds(mp3: bytes) -> float:
    pcm = subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-f", "mp3", "-i", "pipe:0", "-f", "s16le", "-ac", "1", "-ar", "8000", "pipe:1"],
        input=mp3, capture_output=True, check=True,
    ).stdout
    return len(pcm) / 2 / 8000


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_clips_have_no_length_header_and_join_without_losing_audio() -> None:
    def tone(seconds: float) -> bytes:
        # libmp3lame writing to a seekable file would add a Xing header; piped output can't.
        return subprocess.run(
            ["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
             "-c:a", "libmp3lame", "-f", "mp3", "pipe:1"],
            capture_output=True, check=True,
        ).stdout

    first = asyncio.run(apply_audio_filter(tone(1.0), "anull"))
    second = asyncio.run(apply_audio_filter(tone(2.0), "anull"))

    for clip in (first, second):
        assert b"Xing" not in clip[:512] and b"Info" not in clip[:512]
    assert _decoded_seconds(first + second) == pytest.approx(3.0, abs=0.15)


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_an_invalid_filter_raises_rather_than_returning_garbage() -> None:
    tone = subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=0.2",
         "-c:a", "libmp3lame", "-f", "mp3", "pipe:1"],
        capture_output=True, check=True,
    ).stdout

    with pytest.raises(RuntimeError):
        asyncio.run(apply_audio_filter(tone, "not_a_real_filter=1"))
