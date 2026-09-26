from typing import Any

from fastapi.testclient import TestClient

from app.core.siris import SirisAI, get_siris
from app.llm.base import LLMResponse, ToolCall
from app.main import app


def test_health_ok(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["database"] == "ok"


def test_siris_status(client: TestClient) -> None:
    response = client.get("/siris/status")
    assert response.status_code == 200
    body = response.json()
    assert body["app_name"] == "SirisAI"
    assert "echo" in body["tools"]


def test_chat_without_llm_backend_returns_502(client: TestClient) -> None:
    # Default test config has llm_provider="none" -> NullLLMClient.
    response = client.post("/siris/chat", json={"prompt": "hello"})
    assert response.status_code == 502


class _FakeTextLLMClient:
    async def generate(
        self, messages: list[dict[str, Any]], *, tools: list[dict[str, Any]] | None = None, **kwargs: object
    ) -> LLMResponse:
        return LLMResponse(text=f"echo: {messages[-1]['content']}")

    async def aclose(self) -> None:
        pass


class _FakeToolCallingLLMClient:
    """Calls the `echo` tool on the first turn (when offered tools), then
    answers in text on the follow-up call SirisAI makes to summarize the
    tool result (recognizable because `tools` is omitted on that call --
    see `_execute_tool_call` in app/api/routes/siris.py)."""

    async def generate(
        self, messages: list[dict[str, Any]], *, tools: list[dict[str, Any]] | None = None, **kwargs: object
    ) -> LLMResponse:
        if tools:
            return LLMResponse(tool_calls=[ToolCall(name="echo", arguments={"message": messages[-1]["content"]})])
        # The follow-up call's last message is now a fixed honesty nudge
        # (see Cortex's _SYNTHESIS_HONESTY_NUDGE), not the tool result --
        # find the actual tool message by role instead of assuming it's last.
        tool_message = next(m for m in reversed(messages) if m["role"] == "tool")
        return LLMResponse(text="Done -- the tool said: " + tool_message["content"])

    async def aclose(self) -> None:
        pass


class _FakeToolCallingNoFollowUpTextLLMClient:
    """Always calls a tool, even on the follow-up call -- exercises the
    fallback where synthesis doesn't produce text (only `tool_call` is set,
    same as before follow-up synthesis existed)."""

    async def generate(
        self, messages: list[dict[str, Any]], *, tools: list[dict[str, Any]] | None = None, **kwargs: object
    ) -> LLMResponse:
        return LLMResponse(tool_calls=[ToolCall(name="echo", arguments={"message": messages[-1]["content"]})])

    async def aclose(self) -> None:
        pass


def _override_llm(llm: object) -> SirisAI:
    fake_siris = get_siris()
    fake_siris.llm = llm  # type: ignore[assignment]
    app.dependency_overrides[get_siris] = lambda: fake_siris
    return fake_siris


def test_chat_with_backend_configured(client: TestClient) -> None:
    _override_llm(_FakeTextLLMClient())
    try:
        response = client.post("/siris/chat", json={"prompt": "hello"})
        assert response.status_code == 200
        body = response.json()
        assert body["response"] == "echo: hello"
        assert body["tool_call"] is None
        assert isinstance(body["conversation_id"], str) and body["conversation_id"]
    finally:
        app.dependency_overrides.pop(get_siris, None)


def test_chat_executes_tool_call_chosen_by_llm(client: TestClient) -> None:
    """The tool result is fed back to the model for a final natural-language
    answer (`response`) -- the raw call+result is still exposed as
    `tool_call` alongside it, not replaced."""
    _override_llm(_FakeToolCallingLLMClient())
    try:
        response = client.post("/siris/chat", json={"prompt": "please echo this"})
        assert response.status_code == 200
        body = response.json()
        assert body["tool_call"] == {
            "name": "echo",
            "arguments": {"message": "please echo this"},
            "result": {"echo": "please echo this"},
        }
        assert body["response"] == 'Done -- the tool said: {"echo": "please echo this"}'
    finally:
        app.dependency_overrides.pop(get_siris, None)


def test_chat_tool_call_without_follow_up_text_falls_back_to_tool_call_only(client: TestClient) -> None:
    """If the follow-up synthesis call doesn't produce text (e.g. the model
    calls another tool instead, which SirisAI doesn't chain into a loop),
    the response degrades to the pre-synthesis shape: `tool_call` set,
    `response` says what ran instead of leaving clients to show a bare
    "Done." -- still a usable response, not an error."""
    _override_llm(_FakeToolCallingNoFollowUpTextLLMClient())
    try:
        response = client.post("/siris/chat", json={"prompt": "please echo this"})
        assert response.status_code == 200
        body = response.json()
        assert "echo" in body["response"] and "haven't changed anything" in body["response"]
        assert body["tool_call"] == {
            "name": "echo",
            "arguments": {"message": "please echo this"},
            "result": {"echo": "please echo this"},
        }
    finally:
        app.dependency_overrides.pop(get_siris, None)
