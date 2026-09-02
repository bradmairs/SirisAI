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
    async def generate(self, prompt: str, *, tools: list[dict[str, Any]] | None = None, **kwargs: object) -> LLMResponse:
        return LLMResponse(text=f"echo: {prompt}")

    async def aclose(self) -> None:
        pass


class _FakeToolCallingLLMClient:
    async def generate(self, prompt: str, *, tools: list[dict[str, Any]] | None = None, **kwargs: object) -> LLMResponse:
        return LLMResponse(tool_calls=[ToolCall(name="echo", arguments={"message": prompt})])

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
        assert response.json() == {"response": "echo: hello", "tool_call": None}
    finally:
        app.dependency_overrides.pop(get_siris, None)


def test_chat_executes_tool_call_chosen_by_llm(client: TestClient) -> None:
    _override_llm(_FakeToolCallingLLMClient())
    try:
        response = client.post("/siris/chat", json={"prompt": "please echo this"})
        assert response.status_code == 200
        assert response.json() == {
            "response": None,
            "tool_call": {
                "name": "echo",
                "arguments": {"message": "please echo this"},
                "result": {"echo": "please echo this"},
            },
        }
    finally:
        app.dependency_overrides.pop(get_siris, None)
