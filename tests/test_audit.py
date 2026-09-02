from typing import Any

from fastapi.testclient import TestClient

from app.core.siris import SirisAI, get_siris
from app.llm.base import LLMResponse, ToolCall
from app.main import app
from app.plugins.base import ToolPermission


def test_run_tool_records_success() -> None:
    siris = SirisAI(settings=get_siris().settings)
    siris.run_tool("echo", {"message": "hi there"})

    entries = siris.audit.list_recent(limit=5)
    assert entries[0]["tool_name"] == "echo"
    assert entries[0]["arguments"] == {"message": "hi there"}
    assert entries[0]["result"] == {"echo": "hi there"}
    assert entries[0]["error"] is None
    assert entries[0]["duration_ms"] >= 0


def test_run_tool_records_unknown_tool_as_error_and_still_raises() -> None:
    siris = SirisAI(settings=get_siris().settings)

    try:
        siris.run_tool("does_not_exist", {})
        raise AssertionError("expected ValueError")
    except ValueError:
        pass

    entries = siris.audit.list_recent(limit=5)
    assert entries[0]["tool_name"] == "does_not_exist"
    assert entries[0]["result"] is None
    assert "Unknown tool" in entries[0]["error"]


class _FailingTool:
    name = "always_fails"
    description = "A tool that always raises, for audit-log testing."
    permission = ToolPermission.SAFE_ACTION
    parameters = {"type": "object", "properties": {}, "required": []}

    def run(self, **kwargs: Any) -> Any:
        raise RuntimeError("boom")


def test_run_tool_records_tool_run_error() -> None:
    siris = SirisAI(settings=get_siris().settings)
    siris.tools.register(_FailingTool())

    try:
        siris.run_tool("always_fails", {})
        raise AssertionError("expected RuntimeError")
    except RuntimeError:
        pass

    entries = siris.audit.list_recent(limit=5)
    assert entries[0]["tool_name"] == "always_fails"
    assert entries[0]["error"] == "boom"


class _FakeToolCallingLLMClient:
    async def generate(
        self, messages: list[dict[str, Any]], *, tools: list[dict[str, Any]] | None = None, **kwargs: object
    ) -> LLMResponse:
        return LLMResponse(tool_calls=[ToolCall(name="echo", arguments={"message": "audited via chat"})])

    async def aclose(self) -> None:
        pass


def test_chat_tool_call_is_recorded_with_conversation_id(client: TestClient) -> None:
    fake_siris = get_siris()
    fake_siris.llm = _FakeToolCallingLLMClient()  # type: ignore[assignment]
    app.dependency_overrides[get_siris] = lambda: fake_siris
    try:
        response = client.post("/siris/chat", json={"prompt": "please echo this"})
        conversation_id = response.json()["conversation_id"]
    finally:
        app.dependency_overrides.pop(get_siris, None)

    activity = client.get("/siris/activity", params={"limit": 5}).json()
    assert activity[0]["tool_name"] == "echo"
    assert activity[0]["conversation_id"] == conversation_id
    assert activity[0]["result"] == {"echo": "audited via chat"}


def test_activity_endpoint_orders_most_recent_first(client: TestClient) -> None:
    siris = get_siris()
    siris.run_tool("echo", {"message": "first"})
    siris.run_tool("echo", {"message": "second"})

    response = client.get("/siris/activity", params={"limit": 2})
    assert response.status_code == 200
    entries = response.json()
    assert entries[0]["arguments"] == {"message": "second"}
    assert entries[1]["arguments"] == {"message": "first"}
