"""Task planner (milestone 24): lets one chat turn chain multiple tool
calls -- request -> execute step -> verify -> execute next step -> ... ->
respond -- instead of the single-tool-call-per-turn flow
`POST /siris/chat` uses by default (see `_execute_tool_call` in
app/api/routes/siris.py). Opt-in via `ChatRequest.use_planner` on purpose:
this changes what a chat turn can do, so existing callers see no behavior
change unless they ask for it.

Verification here is intentionally the same mechanism that already drives
the loop, not a separate deterministic pass: after each tool result is
appended to the working messages, the next `generate()` call is the model
re-assessing the accumulated evidence against the original request -- if
it isn't satisfied, it calls another tool; if it is, it produces a final
answer. That's a real form of "don't just trust a 200 OK": the model sees
the actual tool result content on every step, not just whether the call
raised. What's explicitly NOT here: a deterministic, tool-specific
verifier that can say "this 200 OK doesn't actually mean the service is
healthy" independent of the model's own judgment -- that needs per-tool
success criteria this milestone doesn't define, and is honestly future
work rather than something this claims to solve.

Every step still goes through `SirisAI.run_tool()` -- full permission
gating, audit logging, and CONFIRM_REQUIRED handling apply exactly as they
do to a single tool call, per step. A plan can't launder its way around
those the same way a skill can't (see app/skills/base.py).
"""

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from app.llm.base import LLMClient
from app.plugins.base import ConfirmationRequired

if TYPE_CHECKING:
    from app.core.siris import SirisAI


@dataclass
class PlanStep:
    name: str
    arguments: dict[str, Any]
    result: Any


@dataclass
class PlanResult:
    steps: list[PlanStep] = field(default_factory=list)
    # The model's final natural-language answer, once it stops calling
    # tools. None if the plan was interrupted (pending_confirmation/error)
    # or ran out of steps without the model producing one.
    final_text: str | None = None
    # Set instead of final_text when a step hit a CONFIRM_REQUIRED tool --
    # steps executed before it are still in `steps`. Resuming the rest of
    # the plan after confirming is out of scope for this milestone; POST
    # /siris/chat/confirm runs just that one step, same as it already does
    # for a single (non-planned) tool call.
    pending_confirmation: tuple[str, dict[str, Any]] | None = None
    # Set instead of final_text when a step raised -- PermissionError
    # (a RESTRICTED tool that isn't enabled) or any other failure.
    # `steps` still holds whatever ran before it.
    error: str | None = None
    permission_denied: bool = False


class Planner:
    def __init__(self, max_steps: int = 5) -> None:
        self.max_steps = max_steps

    async def run(
        self,
        siris: "SirisAI",
        conversation_id: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        llm_client: LLMClient,
    ) -> PlanResult:
        working_messages = list(messages)
        steps: list[PlanStep] = []

        for _ in range(self.max_steps):
            result = await llm_client.generate(working_messages, tools=tools)

            if not result.tool_calls:
                return PlanResult(steps=steps, final_text=result.text)

            call = result.tool_calls[0]
            try:
                output = siris.run_tool(call.name, call.arguments, conversation_id=conversation_id, confirmed=False)
            except ConfirmationRequired:
                return PlanResult(steps=steps, pending_confirmation=(call.name, call.arguments))
            except PermissionError as exc:
                return PlanResult(steps=steps, error=str(exc), permission_denied=True)
            except Exception as exc:
                return PlanResult(steps=steps, error=str(exc))

            steps.append(PlanStep(name=call.name, arguments=call.arguments, result=output))
            working_messages = [
                *working_messages,
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{"function": {"name": call.name, "arguments": call.arguments}}],
                },
                {"role": "tool", "content": json.dumps(output)},
            ]

        # Ran out of steps without the model producing a final answer --
        # surface everything gathered rather than erroring; the caller can
        # still show the raw evidence even without a synthesized answer.
        return PlanResult(steps=steps, final_text=None)
