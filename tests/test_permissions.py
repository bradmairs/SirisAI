from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.siris import SirisAI, get_siris
from app.llm.base import LLMResponse, ToolCall
from app.main import app
from app.plugins.base import ConfirmationRequired, Tool, ToolPermission


class _ConfirmRequiredTool(Tool):
    name = "dangerous_action"
    description = "Test tool requiring confirmation."
    permission = ToolPermission.CONFIRM_REQUIRED
    parameters = {"type": "object", "properties": {"target": {"type": "string"}}, "required": ["target"]}

    def run(self, target: str = "", **kwargs: Any) -> Any:
        return {"acted_on": target}


class _RestrictedTool(Tool):
    name = "wipe_everything"
    description = "Test tool that's RESTRICTED."
    permission = ToolPermission.RESTRICTED
    parameters = {"type": "object", "properties": {}, "required": []}

    def run(self, **kwargs: Any) -> Any:
        return {"wiped": True}


def test_confirm_required_tool_blocks_until_confirmed() -> None:
    siris = SirisAI(settings=Settings())
    siris.tools.register(_ConfirmRequiredTool())

    with pytest.raises(ConfirmationRequired) as exc_info:
        siris.run_tool("dangerous_action", {"target": "the-server"})
    assert exc_info.value.tool_name == "dangerous_action"
    assert exc_info.value.arguments == {"target": "the-server"}

    pending = siris.audit.list_recent(limit=1)[0]
    assert pending["confirmation_required"] is True
    assert pending["result"] is None

    result = siris.run_tool("dangerous_action", {"target": "the-server"}, confirmed=True)
    assert result == {"acted_on": "the-server"}

    ran = siris.audit.list_recent(limit=1)[0]
    assert ran["confirmation_required"] is False
    assert ran["result"] == {"acted_on": "the-server"}


def test_restricted_tool_blocked_unless_allowlisted() -> None:
    blocked = SirisAI(settings=Settings())
    blocked.tools.register(_RestrictedTool())
    with pytest.raises(PermissionError):
        blocked.run_tool("wipe_everything", {})

    allowed = SirisAI(settings=Settings(restricted_tools_enabled="wipe_everything"))
    allowed.tools.register(_RestrictedTool())
    assert allowed.run_tool("wipe_everything", {}) == {"wiped": True}


def test_available_tool_specs_excludes_disabled_restricted_tools() -> None:
    siris = SirisAI(settings=Settings())
    siris.tools.register(_RestrictedTool())
    names = {spec["function"]["name"] for spec in siris.available_tool_specs()}
    assert "wipe_everything" not in names
    assert "echo" in names


def test_status_lists_disabled_restricted_tools() -> None:
    siris = SirisAI(settings=Settings())
    siris.tools.register(_RestrictedTool())
    assert siris.status()["restricted_tools_disabled"] == ["wipe_everything"]


class _FakeConfirmToolCallingLLMClient:
    async def generate(
        self, messages: list[dict[str, Any]], *, tools: list[dict[str, Any]] | None = None, **kwargs: object
    ) -> LLMResponse:
        return LLMResponse(tool_calls=[ToolCall(name="dangerous_action", arguments={"target": "prod-server"})])

    async def aclose(self) -> None:
        pass


def test_chat_surfaces_confirmation_required_without_running_the_tool(client: TestClient) -> None:
    fake_siris = get_siris()
    if fake_siris.tools.get("dangerous_action") is None:
        fake_siris.tools.register(_ConfirmRequiredTool())
    fake_siris.llm = _FakeConfirmToolCallingLLMClient()  # type: ignore[assignment]
    app.dependency_overrides[get_siris] = lambda: fake_siris
    try:
        response = client.post("/siris/chat", json={"prompt": "do the dangerous thing"})
        assert response.status_code == 200
        body = response.json()
        assert body["tool_call"] is None
        assert body["confirmation_required"] == {"name": "dangerous_action", "arguments": {"target": "prod-server"}}
        conversation_id = body["conversation_id"]

        # Not yet run: no tool exchange recorded in the conversation.
        history = client.get(f"/siris/conversations/{conversation_id}").json()
        assert not any(m["role"] == "tool" for m in history)

        confirm_response = client.post(
            "/siris/chat/confirm",
            json={"conversation_id": conversation_id, "tool_name": "dangerous_action", "arguments": {"target": "prod-server"}},
        )
        assert confirm_response.status_code == 200
        confirmed_body = confirm_response.json()
        assert confirmed_body["tool_call"] == {
            "name": "dangerous_action",
            "arguments": {"target": "prod-server"},
            "result": {"acted_on": "prod-server"},
        }

        history_after = client.get(f"/siris/conversations/{conversation_id}").json()
        assert any(m["role"] == "tool" and m["tool_name"] == "dangerous_action" for m in history_after)
    finally:
        app.dependency_overrides.pop(get_siris, None)


def test_home_assistant_call_service_requires_confirmation() -> None:
    from app.plugins.builtin.home_assistant import HomeAssistantCallServiceTool

    assert HomeAssistantCallServiceTool.permission is ToolPermission.CONFIRM_REQUIRED


# -- Tool.needs_confirmation(): a per-call allowlist bypass for an
# otherwise-CONFIRM_REQUIRED tool (see HomeAssistantCallServiceTool for
# the real case this exists for).


class _SelectivelyConfirmedTool(Tool):
    name = "selectively_confirmed_action"
    description = "Test tool -- CONFIRM_REQUIRED, but some calls skip it."
    permission = ToolPermission.CONFIRM_REQUIRED
    parameters = {"type": "object", "properties": {"target": {"type": "string"}}, "required": ["target"]}

    def run(self, target: str = "", **kwargs: Any) -> Any:
        return {"acted_on": target}

    def needs_confirmation(self, arguments: dict[str, Any]) -> bool:
        return arguments.get("target") != "safe-target"


def test_needs_confirmation_override_lets_a_specific_call_skip_confirmation() -> None:
    siris = SirisAI(settings=Settings())
    siris.tools.register(_SelectivelyConfirmedTool())

    result = siris.run_tool("selectively_confirmed_action", {"target": "safe-target"})
    assert result == {"acted_on": "safe-target"}

    ran = siris.audit.list_recent(limit=1)[0]
    assert ran["confirmation_required"] is False
    assert ran["policy"] == "CONFIRM_REQUIRED: this call's arguments are allowlisted to skip confirmation"


def test_needs_confirmation_override_still_blocks_other_calls() -> None:
    siris = SirisAI(settings=Settings())
    siris.tools.register(_SelectivelyConfirmedTool())

    with pytest.raises(ConfirmationRequired):
        siris.run_tool("selectively_confirmed_action", {"target": "risky-target"})

    pending = siris.audit.list_recent(limit=1)[0]
    assert pending["confirmation_required"] is True
    assert pending["policy"] == "CONFIRM_REQUIRED: blocked pending confirmation"

    # Explicit confirmation still runs it, same as before this existed.
    result = siris.run_tool("selectively_confirmed_action", {"target": "risky-target"}, confirmed=True)
    assert result == {"acted_on": "risky-target"}
    confirmed_entry = siris.audit.list_recent(limit=1)[0]
    assert confirmed_entry["policy"] == "CONFIRM_REQUIRED: explicitly confirmed before running"
