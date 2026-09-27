from fastapi.testclient import TestClient

from app.core.siris import get_siris
from app.main import app


def test_transcribe_without_backend_returns_501_saying_it_is_not_configured(client: TestClient) -> None:
    # Default test config has stt_provider="none" -> NullSpeechToTextClient.
    response = client.post("/siris/voice/transcribe", files={"file": ("test.wav", b"fake-audio", "audio/wav")})
    assert response.status_code == 501
    assert "isn't configured" in response.json()["detail"]


def test_speak_without_backend_returns_501_saying_it_is_not_configured(client: TestClient) -> None:
    response = client.post("/siris/voice/speak", json={"text": "hello"})
    assert response.status_code == 501
    assert "isn't configured" in response.json()["detail"]


class _FakeSTTClient:
    async def transcribe(self, audio: bytes, *, filename: str = "audio.wav", content_type: str = "audio/wav") -> str:
        return f"transcribed {len(audio)} bytes from {filename}"

    async def aclose(self) -> None:
        pass


class _FakeTTSClient:
    async def synthesize(self, text: str) -> bytes:
        return f"audio-for:{text}".encode()

    async def aclose(self) -> None:
        pass


def test_transcribe_with_backend_configured(client: TestClient) -> None:
    fake_siris = get_siris()
    fake_siris.stt = _FakeSTTClient()  # type: ignore[assignment]
    app.dependency_overrides[get_siris] = lambda: fake_siris
    try:
        response = client.post(
            "/siris/voice/transcribe", files={"file": ("hello.wav", b"0123456789", "audio/wav")}
        )
        assert response.status_code == 200
        assert response.json() == {"text": "transcribed 10 bytes from hello.wav"}
    finally:
        app.dependency_overrides.pop(get_siris, None)


def test_speak_with_backend_configured(client: TestClient) -> None:
    fake_siris = get_siris()
    fake_siris.tts = _FakeTTSClient()  # type: ignore[assignment]
    app.dependency_overrides[get_siris] = lambda: fake_siris
    try:
        response = client.post("/siris/voice/speak", json={"text": "hello there"})
        assert response.status_code == 200
        assert response.headers["content-type"] == "audio/mpeg"
        assert response.content == b"audio-for:hello there"
    finally:
        app.dependency_overrides.pop(get_siris, None)


def test_speak_pronounces_its_own_name_as_cyrus(client: TestClient) -> None:
    """A direct user request: "Siris" read wrong on TTS, wanted spoken as
    "Cyrus" -- text actually sent to synthesis, not just displayed text,
    should carry the substitution."""
    fake_siris = get_siris()
    fake_siris.tts = _FakeTTSClient()  # type: ignore[assignment]
    app.dependency_overrides[get_siris] = lambda: fake_siris
    try:
        response = client.post("/siris/voice/speak", json={"text": "Hi, I'm SirisAI."})
        assert response.content == b"audio-for:Hi, I'm Cyrus AI."
    finally:
        app.dependency_overrides.pop(get_siris, None)


def test_voice_routes_require_api_key_when_configured(client: TestClient) -> None:
    from app.core.config import Settings, get_settings

    app.dependency_overrides[get_settings] = lambda: Settings(api_key="secret-key")
    try:
        response = client.post("/siris/voice/speak", json={"text": "hi"})
        assert response.status_code == 401
    finally:
        app.dependency_overrides.pop(get_settings, None)
