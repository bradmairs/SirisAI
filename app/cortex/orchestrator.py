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

Persona (a hardening pass, milestone 45): `SIRISAI_PERSONA`, when set, is
prepended as a system message on every turn Cortex drives -- `process()`,
`confirm()`, and the planner loop both feed through -- ahead of context
(16), which stays a separate, `use_context`-gated concern. Empty (the
default) means no persona message at all, the same raw-model-default
behavior as before this milestone existed.

Performance hardening pass: `SirisAI.run_tool()` and `ContextEngine.build()`
are both synchronous, blocking calls (a tool's own HTTP client, `psutil`
sampling, DB queries) -- this process runs a single uvicorn worker/event
loop (see the Dockerfile), so calling either directly from these `async
def` methods would block that one event loop for the call's full
duration, freezing every other concurrent request (another chat turn, a
voice satellite, a proactive check) regardless of what it's doing. Both
are run via `asyncio.to_thread()` instead, so the blocking work happens
on a worker thread and the event loop stays free to handle everything
else meanwhile -- see app/db/session.py's WAL-mode comment for why this
is safe to do concurrently against the same SQLite file.
"""

import asyncio
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

        # Persona (a hardening pass, milestone 45) comes before context --
        # "who you are" ahead of "here's what's currently relevant" -- and
        # applies regardless of use_context, a separate concern (tone, not
        # situational data).
        system_messages: list[dict[str, Any]] = []
        if siris.settings.persona:
            system_messages.append({"role": "system", "content": siris.settings.persona})
        if use_context:
            context_text = await asyncio.to_thread(siris.context.build, siris, prompt)
            if context_text:
                system_messages.append({"role": "system", "content": f"Context (may or may not be relevant):\n{context_text}"})
        messages = [*system_messages, *messages]

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
        self, siris: "SirisAI", conversation_id: str, name: str, arguments: dict[str, Any], *, use_planner: bool = False
    ) -> CortexResult:
        """Actually run a tool call a previous result flagged as
        `pending_confirmation` -- the human has now explicitly approved it.

        `use_planner` is for when that confirmation came from a planner run
        that stopped mid-plan (the original response carried `plan` rather
        than `tool_call` -- see `PlanResult.pending_confirmation`): rather
        than this turn ending after just the one confirmed step, the
        confirmed tool's result is fed into the same kind of loop that
        stopped for it, and `Planner.run()` picks up from there -- further
        tool calls, another `pending_confirmation` if one of those also
        needs approval, or a final answer. `plan` on the result returned is
        the confirmed step followed by whatever the continuation executed
        (this turn's work, same as `process()`'s `plan` is this turn's --
        the earlier steps from before the confirmation are still visible
        via GET /siris/conversations/{id}, not repeated here).

        Without `use_planner` (default), behavior is unchanged from before
        this: the confirmed tool runs, gets one follow-up call for a final
        answer, and the turn ends there -- same as confirming a single
        non-planned tool call always has."""
        messages = self._load_messages(siris, conversation_id)
        if siris.settings.persona:
            messages = [{"role": "system", "content": siris.settings.persona}, *messages]
        confirmed = await self._run_single_tool(
            siris,
            conversation_id,
            name,
            arguments,
            confirmed=True,
            messages=None if use_planner else messages,
            llm_client=None if use_planner else siris.llm,
            trigger="chat: user explicitly confirmed a previously-proposed action",
        )
        if not use_planner or confirmed.error or confirmed.tool_call is None or siris.llm is None:
            return confirmed

        continuation_messages = [
            *messages,
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"function": {"name": name, "arguments": arguments}}],
            },
            {"role": "tool", "content": json.dumps(confirmed.tool_call.result)},
        ]
        rest = await self._run_planned(
            siris, conversation_id, continuation_messages, siris.available_tool_specs(), siris.llm, "general"
        )
        return CortexResult(
            conversation_id=conversation_id,
            response=rest.response,
            plan=[confirmed.tool_call, *rest.plan],
            pending_confirmation=rest.pending_confirmation,
            model_role=rest.model_role,
            error=rest.error,
            permission_denied=rest.permission_denied,
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
            output = await asyncio.to_thread(
                siris.run_tool, name, arguments, conversation_id=conversation_id, confirmed=confirmed, trigger=trigger
            )
        except ConfirmationRequired:
            return CortexResult(conversation_id=conversation_id, pending_confirmation=(name, arguments), model_role=model_role)
        except PermissionError as exc:
            return CortexResult(conversation_id=conversation_id, model_role=model_role, error=str(exc), permission_denied=True)
        except Exception as exc:
            logger.exception("Tool '%s' failed", name)
            # Same follow-up-synthesis treatment as a successful result
            # (below) rather than a bare error straight to the client --
            # live-hit case: unifi_list_clients always raises (the cloud
            # API it talks to has no client-listing endpoint at all), and
            # surfacing that as a raw "Tool 'unifi_list_clients' failed."
            # is exactly the kind of implementation detail the plain-
            # English-only chat UI (see app/web/static/index.html) isn't
            # supposed to show. Falls back to the old hard-error shape
            # only if synthesis itself isn't available or fails too.
            error_payload = {"error": str(exc)}
            siris.conversation.add_message(
                conversation_id, role="tool", content=json.dumps(error_payload), tool_name=name, tool_arguments=arguments
            )
            if llm_client is not None and messages is not None:
                follow_up_messages = [
                    *messages,
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [{"function": {"name": name, "arguments": arguments}}],
                    },
                    {"role": "tool", "content": json.dumps(error_payload)},
                ]
                try:
                    follow_up = await llm_client.generate(follow_up_messages)
                except Exception:
                    logger.exception("Follow-up LLM call after tool '%s' error failed", name)
                else:
                    if follow_up.text:
                        siris.conversation.add_message(conversation_id, role="assistant", content=follow_up.text)
                        return CortexResult(conversation_id=conversation_id, response=follow_up.text, model_role=model_role)
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
