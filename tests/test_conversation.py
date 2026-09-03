from typing import Any

from fastapi.testclient import TestClient

from app.core.siris import get_siris
from app.llm.base import LLMResponse, ToolCall
from app.main import app


class _RecordingLLMClient:
    """Echoes the last user message and records every messages list it was
    called with, so tests can assert what context actually reached the LLM.
    """

    def __init__(self) -> None:
        self.calls: list[list[dict[str, Any]]] = []

    async def generate(
        self, messages: list[dict[str, Any]], *, tools: list[dict[str, Any]] | None = None, **kwargs: object
    ) -> LLMResponse:
        self.calls.append(messages)
        return LLMResponse(text=f"reply #{len(self.calls)}")

    async def aclose(self) -> None:
        pass


def _override_llm(llm: object) -> None:
    fake_siris = get_siris()
    fake_siris.llm = llm  # type: ignore[assignment]
    app.dependency_overrides[get_siris] = lambda: fake_siris


def test_first_chat_call_creates_a_conversation(client: TestClient) -> None:
    fake_llm = _RecordingLLMClient()
    _override_llm(fake_llm)
    try:
        # use_context=False: this test is about conversation history, not
        # the context engine (see tests/test_context_engine.py for that).
        response = client.post("/siris/chat", json={"prompt": "hi", "use_context": False})
        assert response.status_code == 200
        body = response.json()
        assert isinstance(body["conversation_id"], str) and body["conversation_id"]
        assert fake_llm.calls == [[{"role": "user", "content": "hi"}]]
    finally:
        app.dependency_overrides.pop(get_siris, None)


def test_passing_conversation_id_carries_prior_turns_to_the_llm(client: TestClient) -> None:
    fake_llm = _RecordingLLMClient()
    _override_llm(fake_llm)
    try:
        first = client.post("/siris/chat", json={"prompt": "my favorite color is blue", "use_context": False})
        conversation_id = first.json()["conversation_id"]

        second = client.post(
            "/siris/chat",
            json={"prompt": "what did I just say?", "conversation_id": conversation_id, "use_context": False},
        )
        assert second.status_code == 200
        assert second.json()["conversation_id"] == conversation_id

        # Second call's message history should include the first turn's user
        # message and assistant reply, plus the new user message.
        assert fake_llm.calls[1] == [
            {"role": "user", "content": "my favorite color is blue"},
            {"role": "assistant", "content": "reply #1"},
            {"role": "user", "content": "what did I just say?"},
        ]
    finally:
        app.dependency_overrides.pop(get_siris, None)


class _RecordingToolCallingLLMClient:
    """Calls "echo" on the first turn, then just records/echoes text --
    for asserting a later turn's message history includes the earlier
    tool call/result as real chat messages, not just persisted-but-unused
    rows."""

    def __init__(self) -> None:
        self.calls: list[list[dict[str, Any]]] = []
        self._call_count = 0

    async def generate(
        self, messages: list[dict[str, Any]], *, tools: list[dict[str, Any]] | None = None, **kwargs: object
    ) -> LLMResponse:
        self.calls.append(messages)
        self._call_count += 1
        if self._call_count == 1:
            return LLMResponse(tool_calls=[ToolCall(name="echo", arguments={"message": "ping"})])
        if self._call_count == 2:
            return LLMResponse(text="Echoed it.")
        return LLMResponse(text=f"reply #{self._call_count}")

    async def aclose(self) -> None:
        pass


def test_a_prior_tool_call_reaches_the_llm_as_real_history_on_a_later_turn(client: TestClient) -> None:
    fake_llm = _RecordingToolCallingLLMClient()
    _override_llm(fake_llm)
    try:
        first = client.post("/siris/chat", json={"prompt": "please echo ping", "use_context": False})
        conversation_id = first.json()["conversation_id"]
        assert first.json()["tool_call"]["name"] == "echo"

        client.post(
            "/siris/chat",
            json={"prompt": "what did you just do?", "conversation_id": conversation_id, "use_context": False},
        )

        # Third LLM call (second /siris/chat request's generate()) should
        # see the first turn's tool call/result as real chat messages.
        third_call_messages = fake_llm.calls[2]
        assert {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"function": {"name": "echo", "arguments": {"message": "ping"}}}],
        } in third_call_messages
        assert {"role": "tool", "content": '{"echo": "ping"}'} in third_call_messages
    finally:
        app.dependency_overrides.pop(get_siris, None)


def test_unknown_conversation_id_starts_a_new_conversation(client: TestClient) -> None:
    fake_llm = _RecordingLLMClient()
    _override_llm(fake_llm)
    try:
        response = client.post("/siris/chat", json={"prompt": "hi", "conversation_id": "does-not-exist"})
        assert response.status_code == 200
        assert response.json()["conversation_id"] != "does-not-exist"
    finally:
        app.dependency_overrides.pop(get_siris, None)


def test_get_conversation_returns_persisted_messages(client: TestClient) -> None:
    fake_llm = _RecordingLLMClient()
    _override_llm(fake_llm)
    try:
        chat_response = client.post("/siris/chat", json={"prompt": "hello there"})
        conversation_id = chat_response.json()["conversation_id"]
    finally:
        app.dependency_overrides.pop(get_siris, None)

    get_response = client.get(f"/siris/conversations/{conversation_id}")
    assert get_response.status_code == 200
    messages = get_response.json()
    assert [(m["role"], m["content"]) for m in messages] == [
        ("user", "hello there"),
        ("assistant", "reply #1"),
    ]


def test_get_unknown_conversation_returns_404(client: TestClient) -> None:
    response = client.get("/siris/conversations/does-not-exist")
    assert response.status_code == 404
