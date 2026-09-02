import uuid
from typing import Any

from fastapi.testclient import TestClient

from app.core.siris import SirisAI, get_siris
from app.llm.base import LLMResponse, ToolCall
from app.main import app
from app.plugins.base import Tool, ToolPermission


def test_read_tool_policy_is_recorded() -> None:
    siris = SirisAI(settings=get_siris().settings)
    siris.run_tool("echo", {"message": "hi"})
    entry = siris.audit.list_recent(limit=1)[0]
    assert entry["policy"] == "READ: no side effects, always runs"


class _SafeActionTool(Tool):
    name = "test_safe_action"
    description = "test"
    permission = ToolPermission.SAFE_ACTION
    parameters = {"type": "object", "properties": {}, "required": []}

    def run(self, **kwargs: Any) -> Any:
        return {"done": True}


class _ConfirmRequiredTool(Tool):
    name = "test_confirm_required"
    description = "test"
    permission = ToolPermission.CONFIRM_REQUIRED
    parameters = {"type": "object", "properties": {}, "required": []}

    def run(self, **kwargs: Any) -> Any:
        return {"done": True}


def test_safe_action_policy_is_recorded() -> None:
    siris = SirisAI(settings=get_siris().settings)
    siris.tools.register(_SafeActionTool())
    siris.run_tool("test_safe_action", {})
    entry = siris.audit.list_recent(limit=1)[0]
    assert entry["policy"] == "SAFE_ACTION: routine and reversible, runs immediately without confirmation"


def test_confirm_required_policy_differs_before_and_after_confirmation() -> None:
    siris = SirisAI(settings=get_siris().settings)
    siris.tools.register(_ConfirmRequiredTool())

    from app.plugins.base import ConfirmationRequired

    try:
        siris.run_tool("test_confirm_required", {})
    except ConfirmationRequired:
        pass
    blocked = siris.audit.list_recent(limit=1)[0]
    assert blocked["policy"] == "CONFIRM_REQUIRED: blocked pending confirmation"

    siris.run_tool("test_confirm_required", {}, confirmed=True)
    ran = siris.audit.list_recent(limit=1)[0]
    assert ran["policy"] == "CONFIRM_REQUIRED: explicitly confirmed before running"


def test_trigger_and_evidence_are_recorded_when_supplied() -> None:
    siris = SirisAI(settings=get_siris().settings)
    siris.run_tool("echo", {"message": "hi"}, trigger="test: a specific reason", evidence={"why": "because"})
    entry = siris.audit.list_recent(limit=1)[0]
    assert entry["trigger"] == "test: a specific reason"
    assert entry["evidence"] == {"why": "because"}


def test_trigger_and_evidence_default_to_none() -> None:
    siris = SirisAI(settings=get_siris().settings)
    siris.run_tool("echo", {"message": "hi"})
    entry = siris.audit.list_recent(limit=1)[0]
    assert entry["trigger"] is None
    assert entry["evidence"] is None


class _FakeToolCallingLLMClient:
    async def generate(
        self, messages: list[dict[str, Any]], *, tools: list[dict[str, Any]] | None = None, **kwargs: object
    ) -> LLMResponse:
        if tools:
            return LLMResponse(tool_calls=[ToolCall(name="echo", arguments={"message": "hi"})])
        return LLMResponse(text="Done.")

    async def aclose(self) -> None:
        pass


def test_chat_driven_tool_call_records_a_chat_trigger(client: TestClient) -> None:
    fake_siris = get_siris()
    fake_siris.llm = _FakeToolCallingLLMClient()  # type: ignore[assignment]
    app.dependency_overrides[get_siris] = lambda: fake_siris
    try:
        client.post("/siris/chat", json={"prompt": "please echo"})
    finally:
        app.dependency_overrides.pop(get_siris, None)

    entries = fake_siris.audit.list_recent(limit=5)
    echo_entry = next(e for e in entries if e["tool_name"] == "echo")
    assert echo_entry["trigger"] == "chat: model chose this tool while responding to the user's request"


def test_direct_invocation_records_a_non_autonomous_trigger(client: TestClient) -> None:
    client.post("/siris/tools/echo/run", json={"arguments": {"message": "hi"}})
    activity = client.get("/siris/activity", params={"limit": 5}).json()
    echo_entry = next(e for e in activity if e["tool_name"] == "echo")
    assert echo_entry["trigger"] == "direct API request: a human explicitly asked for exactly this tool"


def test_explain_recent_action_tool_finds_and_filters_by_argument(client: TestClient) -> None:
    marker = str(uuid.uuid4())
    client.post("/siris/tools/echo/run", json={"arguments": {"message": f"unrelated-{marker}"}})
    client.post("/siris/tools/echo/run", json={"arguments": {"message": f"plex-{marker}"}})

    response = client.post(
        "/siris/tools/explain_recent_action/run",
        json={"arguments": {"tool_name": "echo", "argument_contains": f"plex-{marker}"}},
    )
    assert response.status_code == 200
    entries = response.json()["tool_call"]["result"]
    assert len(entries) == 1
    assert entries[0]["arguments"] == {"message": f"plex-{marker}"}
    assert entries[0]["policy"] == "READ: no side effects, always runs"


def test_explain_recent_action_tool_always_registered(client: TestClient) -> None:
    response = client.get("/siris/status")
    assert "explain_recent_action" in response.json()["tools"]


def test_proactive_check_notify_call_carries_trigger_and_evidence() -> None:
    class _FakeTools:
        def get(self, name: str) -> object | None:
            return object() if name == "notify" else None

    class _FakeEntities:
        def relationships_for(self, entity_name: str) -> dict[str, list[Any]]:
            return {"outgoing": [], "incoming": []}

    class _FakeSirisForProactive:
        def __init__(self) -> None:
            self.tools = _FakeTools()
            self.entities = _FakeEntities()
            self.calls: list[dict[str, Any]] = []

        def run_tool(self, name: str, arguments: dict[str, Any], **kwargs: Any) -> Any:
            self.calls.append({"name": name, "arguments": arguments, **kwargs})
            if name == "server_health_check":
                return {"healthy": False, "warnings": ["CPU usage is high: 95%"]}
            return {"sent": True}

    from app.api.routes.proactive import run_health_check_and_notify

    fake_siris = _FakeSirisForProactive()
    run_health_check_and_notify(siris=fake_siris)  # type: ignore[arg-type]

    notify_call = next(c for c in fake_siris.calls if c["name"] == "notify")
    assert notify_call["trigger"] == "proactive check: automated schedule (see POST /siris/proactive/check)"
    assert notify_call["evidence"] == [{"message": "CPU usage is high: 95%", "severity": "warning", "action": "notify"}]
