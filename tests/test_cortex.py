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


def _siris(llm: Any) -> SirisAI:
    siris = SirisAI(settings=Settings())
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
