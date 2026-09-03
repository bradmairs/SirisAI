"""Cortex (milestone 33): the single orchestration path a chat turn goes
through -- request -> context -> route -> plan/act -> tool execution ->
persist -> response.

A capstone, not a new subsystem: every piece already existed --
`ContextEngine` (milestone 16), the model router (milestone 20-ish), the
`Planner` (milestone 24), `SirisAI.run_tool()` (milestone 11). Before this
milestone, `POST /siris/chat` assembled all of that inline -- building
context, picking a model, branching on `use_planner`, executing a tool,
making the follow-up call -- with the HTTP route function itself as the
only thing that ever called through the whole pipeline. `Cortex` names and
owns that pipeline instead, decoupled from FastAPI/pydantic (it returns
plain dataclasses, not response models) so it's reusable from anywhere,
not just one route handler. The route in app/api/routes/siris.py is now a
thin adapter: build a `CortexResult`, then shape it into a `ChatResponse`
and raise the right HTTPException if `error` is set.

This is also what makes the LLM client "visibly swappable infrastructure":
callers of Cortex never touch an `LLMClient` directly, `SirisAI.model_router`
decides which one handles a given turn, and Cortex is the only thing that
calls `.generate()` on it.
"""

import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from app.planning.planner import Planner
from app.plugins.base import ConfirmationRequired

if TYPE_CHECKING:
    from app.core.siris import SirisAI

logger = logging.getLogger(__name__)


@dataclass
class ToolCallOutcome:
    name: str
    arguments: dict[str, Any]
    result: Any


@dataclass
class CortexResult:
    conversation_id: str
    # Final natural-language answer, if one was produced.
    response: str | None = None
    # Set when exactly one tool ran this turn (the non-planner path).
    tool_call: ToolCallOutcome | None = None
    # Set when the planner ran -- every step it took, in order. Empty list
    # (not None) when the planner ran but needed no tool.
    plan: list[ToolCallOutcome] = field(default_factory=list)
    # Set instead of response/tool_call when a CONFIRM_REQUIRED tool was
    # chosen -- nothing ran. Re-submit via Cortex.confirm().
    pending_confirmation: tuple[str, dict[str, Any]] | None = None
    model_role: str = "general"
    # Set on failure -- the route layer maps this to an HTTP error. Never
    # raised as an exception: Cortex's own contract is "always return a
    # result", the same way run_tool()'s callers already have to handle
    # ConfirmationRequired/PermissionError/Exception as outcomes, not just
    # the model-choosing-a-tool case.
    error: str | None = None
    permission_denied: bool = False


class Cortex:
    """One request -> response orchestration path for a chat turn. See
    module docstring."""

    def __init__(self, max_plan_steps: int = 5) -> None:
        self.max_plan_steps = max_plan_steps

    async def process(
        self,
        siris: "SirisAI",
        conversation_id: str,
        prompt: str,
        *,
        use_context: bool = True,
        use_tools: bool = True,
        use_planner: bool = False,
        model_role: str | None = None,
    ) -> CortexResult:
        """request -> context -> route -> plan/act -> response, for a new
        user turn. Persists the user's prompt immediately, and whatever
        tool/assistant messages the rest of the pipeline produces."""
        assert siris.llm is not None
        assert siris.model_router is not None

        siris.conversation.add_message(conversation_id, role="user", content=prompt)
        messages = self._load_messages(siris, conversation_id)

        if use_context:
            context_text = siris.context.build(siris, prompt)
            if context_text:
                messages = [
                    {"role": "system", "content": f"Context (may or may not be relevant):\n{context_text}"},
                    *messages,
                ]

        tools = siris.available_tool_specs() if use_tools else None
        resolved_role, llm_client = siris.model_router.pick(siris.llm, prompt, role=model_role)

        if use_planner:
            return await self._run_planned(siris, conversation_id, messages, tools, llm_client, resolved_role)

        try:
            result = await llm_client.generate(messages, tools=tools)
        except Exception:
            logger.exception("LLM generate() failed")
            return CortexResult(conversation_id=conversation_id, model_role=resolved_role, error="LLM backend call failed.")

        if result.tool_calls:
            call = result.tool_calls[0]
            return await self._run_single_tool(
                siris,
                conversation_id,
                call.name,
                call.arguments,
                confirmed=False,
                model_role=resolved_role,
                messages=messages,
                llm_client=llm_client,
                trigger="chat: model chose this tool while responding to the user's request",
            )

        siris.conversation.add_message(conversation_id, role="assistant", content=result.text)
        return CortexResult(conversation_id=conversation_id, response=result.text, model_role=resolved_role)

    async def confirm(
        self, siris: "SirisAI", conversation_id: str, name: str, arguments: dict[str, Any]
    ) -> CortexResult:
        """Actually run a tool call a previous `process()` result flagged as
        `pending_confirmation` -- the human has now explicitly approved it."""
        messages = self._load_messages(siris, conversation_id)
        return await self._run_single_tool(
            siris,
            conversation_id,
            name,
            arguments,
            confirmed=True,
            messages=messages,
            llm_client=siris.llm,
            trigger="chat: user explicitly confirmed a previously-proposed action",
        )

    @staticmethod
    def _load_messages(siris: "SirisAI", conversation_id: str) -> list[dict[str, Any]]:
        """Replay persisted history as real chat messages -- user/assistant
        text verbatim, and a persisted "tool" row expanded back into the
        same assistant-tool_calls + tool-result pair this module builds
        in-turn (_run_single_tool's follow_up_messages, _run_planned's
        working_messages), so a tool call from a prior turn is real
        context on later turns too, not just whatever text the model
        produced about it. This was milestone 10's explicitly-flagged gap
        ("persisted but not yet replayed... full tool-result round-
        tripping remains future work") -- closing it here."""
        history = siris.conversation.get_messages(conversation_id)
        messages: list[dict[str, Any]] = []
        for m in history:
            if m["role"] == "tool":
                messages.append(
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [{"function": {"name": m["tool_name"], "arguments": m["tool_arguments"] or {}}}],
                    }
                )
                messages.append({"role": "tool", "content": m["content"]})
            elif m["role"] in ("user", "assistant"):
                messages.append({"role": m["role"], "content": m["content"]})
        return messages

    async def _run_single_tool(
        self,
        siris: "SirisAI",
        conversation_id: str,
        name: str,
        arguments: dict[str, Any],
        *,
        confirmed: bool,
        model_role: str = "general",
        messages: list[dict[str, Any]] | None = None,
        llm_client: Any = None,
        trigger: str,
    ) -> CortexResult:
        """Run one tool through SirisAI.run_tool() (full permission gating
        and audit logging), persist the exchange, and -- when a
        messages/llm_client are given -- feed the result back to the model
        for a final natural-language answer (a second round-trip). Falls
        back to the tool-call-only result if that follow-up call fails or
        produces no text; a raw tool result is already an acceptable
        answer, the follow-up is a best-effort upgrade."""
        try:
            output = siris.run_tool(name, arguments, conversation_id=conversation_id, confirmed=confirmed, trigger=trigger)
        except ConfirmationRequired:
            return CortexResult(conversation_id=conversation_id, pending_confirmation=(name, arguments), model_role=model_role)
        except PermissionError as exc:
            return CortexResult(conversation_id=conversation_id, model_role=model_role, error=str(exc), permission_denied=True)
        except Exception:
            logger.exception("Tool '%s' failed", name)
            return CortexResult(conversation_id=conversation_id, model_role=model_role, error=f"Tool '{name}' failed.")

        siris.conversation.add_message(
            conversation_id, role="tool", content=json.dumps(output), tool_name=name, tool_arguments=arguments
        )
        tool_call = ToolCallOutcome(name=name, arguments=arguments, result=output)

        if llm_client is not None and messages is not None:
            follow_up_messages = [
                *messages,
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{"function": {"name": name, "arguments": arguments}}],
                },
                {"role": "tool", "content": json.dumps(output)},
            ]
            try:
                # No `tools` offered here on purpose -- this call is meant
                # to produce a final text answer, not choose another tool.
                follow_up = await llm_client.generate(follow_up_messages)
            except Exception:
                logger.exception("Follow-up LLM call after tool '%s' failed", name)
            else:
                if follow_up.text:
                    siris.conversation.add_message(conversation_id, role="assistant", content=follow_up.text)
                    return CortexResult(
                        conversation_id=conversation_id, response=follow_up.text, tool_call=tool_call, model_role=model_role
                    )

        return CortexResult(conversation_id=conversation_id, tool_call=tool_call, model_role=model_role)

    async def _run_planned(
        self,
        siris: "SirisAI",
        conversation_id: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        llm_client: Any,
        model_role: str,
    ) -> CortexResult:
        try:
            plan_result = await Planner(max_steps=self.max_plan_steps).run(siris, conversation_id, messages, tools, llm_client)
        except Exception:
            logger.exception("Planner's LLM generate() failed")
            return CortexResult(conversation_id=conversation_id, model_role=model_role, error="LLM backend call failed.")

        for step in plan_result.steps:
            siris.conversation.add_message(
                conversation_id, role="tool", content=json.dumps(step.result), tool_name=step.name, tool_arguments=step.arguments
            )
        plan = [ToolCallOutcome(name=s.name, arguments=s.arguments, result=s.result) for s in plan_result.steps]

        if plan_result.pending_confirmation:
            name, arguments = plan_result.pending_confirmation
            return CortexResult(
                conversation_id=conversation_id, plan=plan, pending_confirmation=(name, arguments), model_role=model_role
            )

        if plan_result.error:
            return CortexResult(
                conversation_id=conversation_id,
                plan=plan,
                model_role=model_role,
                error=plan_result.error,
                permission_denied=plan_result.permission_denied,
            )

        if plan_result.final_text:
            siris.conversation.add_message(conversation_id, role="assistant", content=plan_result.final_text)

        return CortexResult(conversation_id=conversation_id, response=plan_result.final_text, plan=plan, model_role=model_role)
