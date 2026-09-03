import asyncio
from typing import Any

from fastapi.testclient import TestClient

from app.core.siris import SirisAI, get_siris
from app.llm.base import LLMResponse, ToolCall
from app.main import app
from app.planning.planner import Planner
from app.plugins.base import ConfirmationRequired, Tool, ToolPermission


class _SequenceLLMClient:
    """Returns each response in order, one per generate() call."""

    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = list(responses)

    async def generate(
        self, messages: list[dict[str, Any]], *, tools: list[dict[str, Any]] | None = None, **kwargs: object
    ) -> LLMResponse:
        return self._responses.pop(0)

    async def aclose(self) -> None:
        pass


class _FakeSiris:
    """Just enough of SirisAI's surface for Planner.run() -- a controllable
    run_tool() that records every call it received."""

    def __init__(self, run_tool_fn: Any) -> None:
        self._run_tool_fn = run_tool_fn
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def run_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        conversation_id: str | None = None,
        confirmed: bool = False,
        trigger: str | None = None,
        evidence: Any = None,
    ) -> Any:
        self.calls.append((name, arguments))
        return self._run_tool_fn(name, arguments, conversation_id, confirmed)


def test_planner_stops_immediately_if_model_answers_without_a_tool_call() -> None:
    siris = _FakeSiris(lambda *a: (_ for _ in ()).throw(AssertionError("run_tool should not be called")))
    planner = Planner(max_steps=5)

    result = asyncio.run(
        planner.run(siris, "conv-1", [], tools=None, llm_client=_SequenceLLMClient([LLMResponse(text="hi")]))
    )

    assert result.steps == []
    assert result.final_text == "hi"
    assert result.pending_confirmation is None
    assert result.error is None


def test_planner_chains_multiple_tool_calls_then_produces_final_text() -> None:
    siris = _FakeSiris(lambda name, arguments, *_: {"echo": arguments["message"]})
    responses = [
        LLMResponse(tool_calls=[ToolCall(name="echo", arguments={"message": "first"})]),
        LLMResponse(tool_calls=[ToolCall(name="echo", arguments={"message": "second"})]),
        LLMResponse(text="All done."),
    ]

    result = asyncio.run(
        Planner(max_steps=5).run(siris, "conv-1", [], tools=[], llm_client=_SequenceLLMClient(responses))
    )

    assert [s.name for s in result.steps] == ["echo", "echo"]
    assert [s.arguments["message"] for s in result.steps] == ["first", "second"]
    assert [s.result for s in result.steps] == [{"echo": "first"}, {"echo": "second"}]
    assert result.final_text == "All done."
    assert siris.calls == [("echo", {"message": "first"}), ("echo", {"message": "second"})]


def test_planner_stops_on_confirmation_required_and_keeps_prior_steps() -> None:
    def run_tool_fn(name: str, arguments: dict[str, Any], *_: Any) -> Any:
        if name == "risky":
            raise ConfirmationRequired(name, arguments)
        return {"ok": True}

    siris = _FakeSiris(run_tool_fn)
    responses = [
        LLMResponse(tool_calls=[ToolCall(name="safe", arguments={})]),
        LLMResponse(tool_calls=[ToolCall(name="risky", arguments={"target": "plex"})]),
    ]

    result = asyncio.run(
        Planner(max_steps=5).run(siris, "conv-1", [], tools=[], llm_client=_SequenceLLMClient(responses))
    )

    assert [s.name for s in result.steps] == ["safe"]
    assert result.pending_confirmation == ("risky", {"target": "plex"})
    assert result.final_text is None
    assert result.error is None


def test_planner_stops_on_permission_error() -> None:
    def run_tool_fn(name: str, arguments: dict[str, Any], *_: Any) -> Any:
        raise PermissionError("Tool 'restricted_thing' is restricted and not enabled.")

    siris = _FakeSiris(run_tool_fn)
    responses = [LLMResponse(tool_calls=[ToolCall(name="restricted_thing", arguments={})])]

    result = asyncio.run(
        Planner(max_steps=5).run(siris, "conv-1", [], tools=[], llm_client=_SequenceLLMClient(responses))
    )

    assert result.steps == []
    assert result.permission_denied is True
    assert "restricted" in result.error


def test_planner_stops_on_other_tool_failure() -> None:
    def run_tool_fn(name: str, arguments: dict[str, Any], *_: Any) -> Any:
        raise RuntimeError("boom")

    siris = _FakeSiris(run_tool_fn)
    responses = [LLMResponse(tool_calls=[ToolCall(name="flaky", arguments={})])]

    result = asyncio.run(
        Planner(max_steps=5).run(siris, "conv-1", [], tools=[], llm_client=_SequenceLLMClient(responses))
    )

    assert result.error == "boom"
    assert result.permission_denied is False


def test_planner_gives_up_after_max_steps_without_a_final_answer() -> None:
    siris = _FakeSiris(lambda name, arguments, *_: {"echo": arguments["message"]})
    responses = [
        LLMResponse(tool_calls=[ToolCall(name="echo", arguments={"message": "one"})]),
        LLMResponse(tool_calls=[ToolCall(name="echo", arguments={"message": "two"})]),
    ]

    result = asyncio.run(
        Planner(max_steps=2).run(siris, "conv-1", [], tools=[], llm_client=_SequenceLLMClient(responses))
    )

    assert len(result.steps) == 2
    assert result.final_text is None
    assert result.pending_confirmation is None
    assert result.error is None


# -- Endpoint-level: exercises the real SirisAI/echo tool through POST /siris/chat.


class _FakeMultiStepPlannerLLMClient:
    def __init__(self) -> None:
        self.call_count = 0

    async def generate(
        self, messages: list[dict[str, Any]], *, tools: list[dict[str, Any]] | None = None, **kwargs: object
    ) -> LLMResponse:
        self.call_count += 1
        if self.call_count <= 2:
            return LLMResponse(tool_calls=[ToolCall(name="echo", arguments={"message": f"step {self.call_count}"})])
        return LLMResponse(text="All done.")

    async def aclose(self) -> None:
        pass


def _override_llm(llm: object) -> SirisAI:
    fake_siris = get_siris()
    fake_siris.llm = llm  # type: ignore[assignment]
    app.dependency_overrides[get_siris] = lambda: fake_siris
    return fake_siris


def test_chat_with_planner_chains_two_tool_calls(client: TestClient) -> None:
    _override_llm(_FakeMultiStepPlannerLLMClient())
    try:
        response = client.post("/siris/chat", json={"prompt": "do two things", "use_planner": True})
        assert response.status_code == 200
        body = response.json()
        assert body["tool_call"] is None
        assert [step["name"] for step in body["plan"]] == ["echo", "echo"]
        assert [step["arguments"]["message"] for step in body["plan"]] == ["step 1", "step 2"]
        assert body["response"] == "All done."
    finally:
        app.dependency_overrides.pop(get_siris, None)


def test_chat_without_planner_still_stops_at_one_tool_call(client: TestClient) -> None:
    """use_planner defaults to False -- confirms existing behavior (one
    tool call max, no `plan` field) is unchanged for callers who don't
    opt in."""
    _override_llm(_FakeMultiStepPlannerLLMClient())
    try:
        response = client.post("/siris/chat", json={"prompt": "do two things"})
        assert response.status_code == 200
        body = response.json()
        assert body["plan"] is None
        assert body["tool_call"]["name"] == "echo"
    finally:
        app.dependency_overrides.pop(get_siris, None)


# -- Endpoint-level: POST /siris/chat/confirm continuing a plan (Cortex.confirm's
# use_planner path) rather than just running one confirmed step and stopping.


class _ConfirmRequiredTool(Tool):
    name = "dangerous_action"
    description = "Test tool requiring confirmation."
    permission = ToolPermission.CONFIRM_REQUIRED
    parameters = {"type": "object", "properties": {"target": {"type": "string"}}, "required": ["target"]}

    def run(self, target: str = "", **kwargs: Any) -> Any:
        return {"acted_on": target}


class _FakePlannerConfirmThenContinueLLMClient:
    def __init__(self) -> None:
        self.call_count = 0

    async def generate(
        self, messages: list[dict[str, Any]], *, tools: list[dict[str, Any]] | None = None, **kwargs: object
    ) -> LLMResponse:
        self.call_count += 1
        if self.call_count == 1:
            return LLMResponse(tool_calls=[ToolCall(name="dangerous_action", arguments={"target": "prod-server"})])
        if self.call_count == 2:
            return LLMResponse(tool_calls=[ToolCall(name="echo", arguments={"message": "after confirm"})])
        return LLMResponse(text="Plan complete.")

    async def aclose(self) -> None:
        pass


def test_chat_confirm_with_use_planner_continues_the_plan_over_http(client: TestClient) -> None:
    fake_siris = _override_llm(_FakePlannerConfirmThenContinueLLMClient())
    if fake_siris.tools.get("dangerous_action") is None:
        fake_siris.tools.register(_ConfirmRequiredTool())
    try:
        first = client.post("/siris/chat", json={"prompt": "do the risky thing then echo", "use_planner": True})
        assert first.status_code == 200
        first_body = first.json()
        assert first_body["tool_call"] is None
        assert first_body["plan"] is None  # nothing ran yet -- stopped immediately for confirmation
        assert first_body["confirmation_required"] == {"name": "dangerous_action", "arguments": {"target": "prod-server"}}
        conversation_id = first_body["conversation_id"]

        confirm = client.post(
            "/siris/chat/confirm",
            json={
                "conversation_id": conversation_id,
                "tool_name": "dangerous_action",
                "arguments": {"target": "prod-server"},
                "use_planner": True,
            },
        )
        assert confirm.status_code == 200
        confirm_body = confirm.json()
        assert [step["name"] for step in confirm_body["plan"]] == ["dangerous_action", "echo"]
        assert confirm_body["response"] == "Plan complete."
        assert confirm_body["confirmation_required"] is None

        history = client.get(f"/siris/conversations/{conversation_id}").json()
        assert [m["role"] for m in history] == ["user", "tool", "tool", "assistant"]
    finally:
        app.dependency_overrides.pop(get_siris, None)
