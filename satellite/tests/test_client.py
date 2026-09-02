import json

import httpx

from siris_satellite.client import SirisClient


def _mock_client(handler) -> SirisClient:
    return SirisClient(base_url="http://localhost:8000", api_key="test-key", transport=httpx.MockTransport(handler))


def test_transcribe_uploads_audio_and_returns_text() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/siris/voice/transcribe"
        assert request.headers["authorization"] == "Bearer test-key"
        return httpx.Response(200, json={"text": "turn on the lights"})

    client = _mock_client(handler)
    assert client.transcribe(b"fake-wav-bytes") == "turn on the lights"
    client.close()


def test_chat_sends_prompt_and_returns_response_text() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/siris/chat"
        body = json.loads(request.content)
        assert body == {"prompt": "hello", "conversation_id": None}
        return httpx.Response(200, json={"conversation_id": "abc-123", "response": "hi there", "tool_call": None})

    client = _mock_client(handler)
    assert client.chat("hello") == "hi there"
    client.close()


def test_chat_carries_conversation_id_across_calls() -> None:
    seen_conversation_ids = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen_conversation_ids.append(body["conversation_id"])
        return httpx.Response(
            200, json={"conversation_id": "abc-123", "response": f"reply #{len(seen_conversation_ids)}", "tool_call": None}
        )

    client = _mock_client(handler)
    client.chat("first")
    client.chat("second")
    client.close()

    assert seen_conversation_ids == [None, "abc-123"]


def test_chat_describes_a_tool_call_result() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "conversation_id": "abc-123",
                "response": None,
                "tool_call": {"name": "echo", "arguments": {}, "result": {}},
            },
        )

    client = _mock_client(handler)
    assert client.chat("do a thing") == "Done: echo."
    client.close()


def test_chat_describes_a_pending_confirmation() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "conversation_id": "abc-123",
                "response": None,
                "tool_call": None,
                "confirmation_required": {"name": "docker_restart_container", "arguments": {}},
            },
        )

    client = _mock_client(handler)
    result = client.chat("restart plex")
    assert "docker_restart_container" in result
    assert "confirmation" in result.lower()
    client.close()


def test_speak_returns_audio_bytes() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/siris/voice/speak"
        body = json.loads(request.content)
        assert body == {"text": "hello there"}
        return httpx.Response(200, content=b"fake-mp3-bytes")

    client = _mock_client(handler)
    assert client.speak("hello there") == b"fake-mp3-bytes"
    client.close()
