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
        assert body == {"prompt": "hello", "conversation_id": None, "room": None}
        return httpx.Response(200, json={"conversation_id": "abc-123", "response": "hi there", "tool_call": None})

    client = _mock_client(handler)
    assert client.chat("hello") == "hi there"
    client.close()


def test_chat_sends_room_when_configured() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["room"] == "kitchen"
        return httpx.Response(200, json={"conversation_id": "abc-123", "response": "hi there", "tool_call": None})

    client = SirisClient(base_url="http://localhost:8000", room="kitchen", transport=httpx.MockTransport(handler))
    client.chat("hello")
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


def test_register_posts_room_and_callback_url() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/siris/satellites/register"
        body = json.loads(request.content)
        assert body == {"room": "kitchen", "callback_url": "http://192.168.1.50:8090"}
        return httpx.Response(200, json={"id": 1, "room": "kitchen", "callback_url": "http://192.168.1.50:8090", "last_active_at": "2026-01-01T00:00:00"})

    client = _mock_client(handler)
    client.register("kitchen", "http://192.168.1.50:8090")  # must not raise
    client.close()


def test_register_raises_on_backend_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502)

    client = _mock_client(handler)
    try:
        client.register("kitchen", "http://192.168.1.50:8090")
        raised = False
    except httpx.HTTPStatusError:
        raised = True
    client.close()
    assert raised


# -- converse (POST /siris/voice/converse, streamed NDJSON)


def test_converse_streams_events_and_carries_conversation_id() -> None:
    seen: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/siris/voice/converse"
        seen.append(request.content)
        lines = [
            {"type": "transcript", "text": "hi"},
            {"type": "sentence", "text": "Hello.", "audio": "bXAz"},
            {"type": "final", "conversation_id": "conv-9", "response": "Hello.", "expects_reply": False},
        ]
        return httpx.Response(200, content="".join(json.dumps(line) + "\n" for line in lines).encode())

    client = SirisClient(base_url="http://localhost:8000", room="den", user="brad", transport=httpx.MockTransport(handler))
    events = list(client.converse(b"wav-bytes"))
    assert [e["type"] for e in events] == ["transcript", "sentence", "final"]
    list(client.converse(b"more"))
    client.close()

    assert b'name="room"' in seen[0] and b"den" in seen[0]
    assert b"wav-bytes" in seen[0]
    assert b"conversation_id" not in seen[0]
    assert b'name="conversation_id"' in seen[1] and b"conv-9" in seen[1]


def test_converse_sends_the_configured_user_header() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-siris-user"] == "brad"
        return httpx.Response(200, content=b'{"type": "final", "response": null}\n')

    client = SirisClient(base_url="http://localhost:8000", user="brad", transport=httpx.MockTransport(handler))
    list(client.converse(b"x"))
    client.close()


def test_converse_on_an_older_server_raises_unsupported() -> None:
    from siris_satellite.client import ConverseUnsupported

    client = _mock_client(lambda request: httpx.Response(404))
    try:
        list(client.converse(b"x"))
        raised = False
    except ConverseUnsupported:
        raised = True
    client.close()
    assert raised
