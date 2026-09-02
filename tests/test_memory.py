import uuid
from typing import Any

from fastapi.testclient import TestClient

from app.core.siris import get_siris
from app.llm.base import LLMResponse, ToolCall
from app.main import app


def test_remember_then_recall_via_endpoints(client: TestClient) -> None:
    content = f"favorite homelab distro is Proxmox {uuid.uuid4()}"

    create_response = client.post("/siris/memory", json={"content": content})
    assert create_response.status_code == 200
    created = create_response.json()
    assert created["content"] == content
    assert isinstance(created["id"], int)

    list_response = client.get("/siris/memory", params={"query": content})
    assert list_response.status_code == 200
    entries = list_response.json()
    assert [e["content"] for e in entries] == [content]


class _FakeRememberingLLMClient:
    def __init__(self, content: str) -> None:
        self._content = content

    async def generate(
        self, messages: list[dict[str, Any]], *, tools: list[dict[str, Any]] | None = None, **kwargs: object
    ) -> LLMResponse:
        return LLMResponse(tool_calls=[ToolCall(name="remember", arguments={"content": self._content})])

    async def aclose(self) -> None:
        pass


def test_chat_can_write_memory_via_remember_tool(client: TestClient) -> None:
    content = f"user is building a k3s cluster {uuid.uuid4()}"
    fake_siris = get_siris()
    fake_siris.llm = _FakeRememberingLLMClient(content)  # type: ignore[assignment]
    app.dependency_overrides[get_siris] = lambda: fake_siris
    try:
        chat_response = client.post("/siris/chat", json={"prompt": "remember that I'm building a k3s cluster"})
        assert chat_response.status_code == 200
        assert chat_response.json()["tool_call"]["name"] == "remember"
    finally:
        app.dependency_overrides.pop(get_siris, None)

    list_response = client.get("/siris/memory", params={"query": content})
    assert [e["content"] for e in list_response.json()] == [content]


def test_siris_status_lists_memory_tools(client: TestClient) -> None:
    response = client.get("/siris/status")
    tools = response.json()["tools"]
    assert "remember" in tools
    assert "recall" in tools
