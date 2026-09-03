"""Unit-level tests for Cortex (milestone 33) -- exercised directly rather
than through POST /siris/chat, which is now a thin adapter over this (see
tests/test_planner.py, test_permissions.py, test_context_engine.py, etc.
for the endpoint-level coverage of the same flows through HTTP).
"""

import asyncio
from typing import Any

from app.core.config import Settings
from app.core.siris import SirisAI
from app.cortex.orchestrator import Cortex
from app.llm.base import LLMResponse, ToolCall
from app.plugins.base import Tool, ToolPermission


class _FailingTool(Tool):
    name = "flaky_lookup"
    description = "Test tool that always raises."
    permission = ToolPermission.READ
    parameters = {"type": "object", "properties": {}, "required": []}

    def run(self, **kwargs: Any) -> Any:
        raise RuntimeError("cloud API has no client-listing endpoint")


class _ConfirmRequiredTool(Tool):
    name = "dangerous_action"
    description = "Test tool requiring confirmation."
    permission = ToolPermission.CONFIRM_REQUIRED
    parameters = {"type": "object", "properties": {"target": {"type": "string"}}, "required": ["target"]}

    def run(self, target: str = "", **kwargs: Any) -> Any:
        return {"acted_on": target}


class _FixedLLMClient:
    def __init__(self, response: LLMResponse) -> None:
        self._response = response

    async def generate(
        self, messages: list[dict[str, Any]], *, tools: list[dict[str, Any]] | None = None, **kwargs: object
    ) -> LLMResponse:
        return self._response

    async def aclose(self) -> None:
        pass


class _SequenceLLMClient:
    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = list(responses)

    async def generate(
        self, messages: list[dict[str, Any]], *, tools: list[dict[str, Any]] | None = None, **kwargs: object
    ) -> LLMResponse:
        return self._responses.pop(0)

    async def aclose(self) -> None:
        pass


class _FailingLLMClient:
    async def generate(
        self, messages: list[dict[str, Any]], *, tools: list[dict[str, Any]] | None = None, **kwargs: object
    ) -> LLMResponse:
        raise RuntimeError("backend unreachable")

    async def aclose(self) -> None:
        pass


class _RecordingLLMClient:
    """Records every messages list it's called with and returns queued
    responses in order -- for asserting exactly what history reached the
    model on a later call."""

    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[list[dict[str, Any]]] = []

    async def generate(
        self, messages: list[dict[str, Any]], *, tools: list[dict[str, Any]] | None = None, **kwargs: object
    ) -> LLMResponse:
        self.calls.append(messages)
        return self._responses.pop(0)

    async def aclose(self) -> None:
        pass


def _siris(llm: Any, settings: Settings | None = None) -> SirisAI:
    siris = SirisAI(settings=settings or Settings())
    siris.llm = llm
    return siris


def test_process_with_no_tool_call_returns_text_and_persists_turn() -> None:
    siris = _siris(_FixedLLMClient(LLMResponse(text="hi there")))
    conversation_id = siris.conversation.create_conversation()

    result = asyncio.run(Cortex().process(siris, conversation_id, "hello", use_context=False, use_tools=False))

    assert result.response == "hi there"
    assert result.tool_call is None
    assert result.plan == []
    assert result.error is None

    history = siris.conversation.get_messages(conversation_id)
    assert [m["role"] for m in history] == ["user", "assistant"]
    assert history[-1]["content"] == "hi there"


def test_process_runs_a_tool_then_feeds_result_back_for_final_text() -> None:
    responses = _SequenceLLMClient(
        [
            LLMResponse(tool_calls=[ToolCall(name="echo", arguments={"message": "ping"})]),
            LLMResponse(text="Echoed ping for you."),
        ]
    )
    siris = _siris(responses)
    conversation_id = siris.conversation.create_conversation()

    result = asyncio.run(Cortex().process(siris, conversation_id, "please echo", use_context=False))

    assert result.tool_call is not None
    assert result.tool_call.name == "echo"
    assert result.response == "Echoed ping for you."

    history = siris.conversation.get_messages(conversation_id)
    assert [m["role"] for m in history] == ["user", "tool", "assistant"]


def test_process_tool_failure_is_explained_in_plain_language_not_raised() -> None:
    """Live-hit case (2026-09): unifi_list_clients always raises since the
    cloud API it talks to has no client-listing endpoint. That shouldn't
    surface as a raw "Tool 'X' failed." error to a plain-English-only
    chat UI -- it should get the same follow-up-synthesis treatment a
    successful result does."""
    responses = _SequenceLLMClient(
        [
            LLMResponse(tool_calls=[ToolCall(name="flaky_lookup", arguments={})]),
            LLMResponse(text="I couldn't check that -- that data isn't available right now."),
        ]
    )
    siris = _siris(responses)
    siris.tools.register(_FailingTool())
    conversation_id = siris.conversation.create_conversation()

    result = asyncio.run(Cortex().process(siris, conversation_id, "who's connected?", use_context=False))

    assert result.error is None
    assert result.response == "I couldn't check that -- that data isn't available right now."
    assert result.tool_call is None

    history = siris.conversation.get_messages(conversation_id)
    assert [m["role"] for m in history] == ["user", "tool", "assistant"]
    assert "cloud API has no client-listing endpoint" in history[1]["content"]


def test_process_tool_failure_falls_back_to_error_when_synthesis_produces_no_text() -> None:
    responses = _SequenceLLMClient(
        [
            LLMResponse(tool_calls=[ToolCall(name="flaky_lookup", arguments={})]),
            LLMResponse(text=None),
        ]
    )
    siris = _siris(responses)
    siris.tools.register(_FailingTool())
    conversation_id = siris.conversation.create_conversation()

    result = asyncio.run(Cortex().process(siris, conversation_id, "who's connected?", use_context=False))

    assert result.response is None
    assert result.error == "Tool 'flaky_lookup' failed."


def test_process_with_use_planner_delegates_to_planner_and_returns_plan() -> None:
    responses = _SequenceLLMClient(
        [
            LLMResponse(tool_calls=[ToolCall(name="echo", arguments={"message": "one"})]),
            LLMResponse(text="Done."),
        ]
    )
    siris = _siris(responses)
    conversation_id = siris.conversation.create_conversation()

    result = asyncio.run(Cortex(max_plan_steps=5).process(siris, conversation_id, "do a thing", use_context=False, use_planner=True))

    assert [step.name for step in result.plan] == ["echo"]
    assert result.response == "Done."
    assert result.tool_call is None


def test_process_returns_error_result_when_llm_call_fails() -> None:
    siris = _siris(_FailingLLMClient())
    conversation_id = siris.conversation.create_conversation()

    result = asyncio.run(Cortex().process(siris, conversation_id, "hello", use_context=False, use_tools=False))

    assert result.error == "LLM backend call failed."
    assert result.response is None


# -- Persona (a hardening pass, milestone 45)


def test_process_prepends_persona_when_configured() -> None:
    llm = _RecordingLLMClient([LLMResponse(text="hi there")])
    siris = _siris(llm, settings=Settings(persona="You are Jarvis: dry wit, always precise."))
    conversation_id = siris.conversation.create_conversation()

    asyncio.run(Cortex().process(siris, conversation_id, "hello", use_context=False))

    sent = llm.calls[0]
    assert sent[0] == {"role": "system", "content": "You are Jarvis: dry wit, always precise."}
    assert sent[-1] == {"role": "user", "content": "hello"}


def test_process_omits_persona_message_when_not_configured() -> None:
    llm = _RecordingLLMClient([LLMResponse(text="hi there")])
    siris = _siris(llm)  # default Settings() -- empty persona
    conversation_id = siris.conversation.create_conversation()

    asyncio.run(Cortex().process(siris, conversation_id, "hello", use_context=False))

    assert all(m["role"] != "system" for m in llm.calls[0])


def test_process_puts_persona_before_context() -> None:
    llm = _RecordingLLMClient([LLMResponse(text="hi there")])
    siris = _siris(llm, settings=Settings(persona="You are Jarvis."))
    conversation_id = siris.conversation.create_conversation()

    asyncio.run(Cortex().process(siris, conversation_id, "hello", use_context=True))

    system_messages = [m for m in llm.calls[0] if m["role"] == "system"]
    assert len(system_messages) == 2
    assert system_messages[0]["content"] == "You are Jarvis."
    assert system_messages[1]["content"].startswith("Context")


def test_confirm_prepends_persona_when_configured() -> None:
    llm = _RecordingLLMClient([LLMResponse(text="Confirmed and done.")])
    siris = _siris(llm, settings=Settings(persona="You are Jarvis."))
    conversation_id = siris.conversation.create_conversation()
    siris.conversation.add_message(conversation_id, role="user", content="do the risky thing")

    asyncio.run(Cortex().confirm(siris, conversation_id, "echo", {"message": "confirmed"}))

    follow_up_messages = llm.calls[0]
    assert follow_up_messages[0] == {"role": "system", "content": "You are Jarvis."}


def test_a_prior_turns_tool_call_is_replayed_as_real_history_on_the_next_turn() -> None:
    """The gap milestone 10 flagged and Cortex._load_messages now closes:
    a tool call from an earlier turn shows up on a later turn as the same
    assistant-tool_calls + tool-result pair the in-turn follow-up call
    already uses, not just whatever text the model said about it."""
    llm = _RecordingLLMClient(
        [
            LLMResponse(tool_calls=[ToolCall(name="echo", arguments={"message": "ping"})]),
            LLMResponse(text="Echoed ping for you."),
            LLMResponse(text="You asked me to echo 'ping' last turn."),
        ]
    )
    siris = _siris(llm)
    conversation_id = siris.conversation.create_conversation()

    asyncio.run(Cortex().process(siris, conversation_id, "please echo ping", use_context=False))
    result = asyncio.run(Cortex().process(siris, conversation_id, "what did you just do?", use_context=False))

    assert result.response == "You asked me to echo 'ping' last turn."
    second_turn_messages = llm.calls[-1]
    assert {"role": "user", "content": "please echo ping"} in second_turn_messages
    assert {
        "role": "assistant",
        "content": "",
        "tool_calls": [{"function": {"name": "echo", "arguments": {"message": "ping"}}}],
    } in second_turn_messages
    assert {"role": "tool", "content": '{"echo": "ping"}'} in second_turn_messages
    assert {"role": "assistant", "content": "Echoed ping for you."} in second_turn_messages
    assert second_turn_messages[-1] == {"role": "user", "content": "what did you just do?"}


def test_confirm_runs_the_tool_and_persists_without_a_fresh_user_message() -> None:
    siris = _siris(_FixedLLMClient(LLMResponse(text="Confirmed and done.")))
    conversation_id = siris.conversation.create_conversation()
    siris.conversation.add_message(conversation_id, role="user", content="do the risky thing")

    result = asyncio.run(Cortex().confirm(siris, conversation_id, "echo", {"message": "confirmed"}))

    assert result.tool_call is not None
    assert result.tool_call.result == {"echo": "confirmed"}
    history = siris.conversation.get_messages(conversation_id)
    assert [m["role"] for m in history] == ["user", "tool", "assistant"]


def test_confirm_with_use_planner_continues_the_plan_after_the_confirmed_step() -> None:
    """A plan that stopped for confirmation, once confirmed with
    use_planner=True, keeps going instead of ending the turn after just
    the one step -- further tool calls run, and the final answer only
    comes back once the model is satisfied."""
    llm = _SequenceLLMClient(
        [
            LLMResponse(tool_calls=[ToolCall(name="echo", arguments={"message": "step 2"})]),
            LLMResponse(text="All done after confirming."),
        ]
    )
    siris = _siris(llm)
    siris.tools.register(_ConfirmRequiredTool())
    conversation_id = siris.conversation.create_conversation()
    siris.conversation.add_message(conversation_id, role="user", content="do the risky thing then echo")

    result = asyncio.run(
        Cortex().confirm(siris, conversation_id, "dangerous_action", {"target": "prod-server"}, use_planner=True)
    )

    assert [step.name for step in result.plan] == ["dangerous_action", "echo"]
    assert result.plan[0].result == {"acted_on": "prod-server"}
    assert result.plan[1].result == {"echo": "step 2"}
    assert result.response == "All done after confirming."
    assert result.pending_confirmation is None
    assert result.error is None

    history = siris.conversation.get_messages(conversation_id)
    assert [m["role"] for m in history] == ["user", "tool", "tool", "assistant"]


def test_confirm_with_use_planner_can_stop_on_another_confirmation_required() -> None:
    llm = _SequenceLLMClient([LLMResponse(tool_calls=[ToolCall(name="dangerous_action", arguments={"target": "db"})])])
    siris = _siris(llm)
    siris.tools.register(_ConfirmRequiredTool())
    conversation_id = siris.conversation.create_conversation()
    siris.conversation.add_message(conversation_id, role="user", content="do two risky things")

    result = asyncio.run(
        Cortex().confirm(siris, conversation_id, "dangerous_action", {"target": "web-server"}, use_planner=True)
    )

    # The first (already-confirmed) step shows up in `plan`; the second one
    # never ran -- it's surfaced as a fresh pending_confirmation instead.
    assert [step.name for step in result.plan] == ["dangerous_action"]
    assert result.plan[0].arguments == {"target": "web-server"}
    assert result.pending_confirmation == ("dangerous_action", {"target": "db"})
    assert result.response is None


def test_confirm_without_use_planner_does_not_continue_even_with_a_confirm_required_tool() -> None:
    """use_planner defaults to False -- confirming a tool that happens to
    have come from a plan, without opting in, behaves exactly like
    confirming any other single tool call: one follow-up call, done."""
    siris = _siris(_FixedLLMClient(LLMResponse(text="Confirmed and done.")))
    siris.tools.register(_ConfirmRequiredTool())
    conversation_id = siris.conversation.create_conversation()

    result = asyncio.run(Cortex().confirm(siris, conversation_id, "dangerous_action", {"target": "prod-server"}))

    assert result.plan == []
    assert result.tool_call is not None
    assert result.tool_call.name == "dangerous_action"
    assert result.response == "Confirmed and done."
