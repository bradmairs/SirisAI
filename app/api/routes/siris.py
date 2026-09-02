"""Routes for the SirisAI core service itself.

Kept separate from `health.py` on purpose: `/health` is an infra-level
liveness probe, `/siris/status` is app-level introspection (what's
registered, what backend is active) that will grow as the core does.
"""

import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.core.auth import require_api_key
from app.core.siris import SirisAI, get_siris
from app.llm.base import LLMClient
from app.planning.planner import Planner
from app.plugins.base import ConfirmationRequired

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/siris", tags=["siris"], dependencies=[Depends(require_api_key)])


@router.get("/status")
def status(siris: SirisAI = Depends(get_siris)) -> dict[str, object]:
    return siris.status()


class ToolInfo(BaseModel):
    name: str
    description: str
    parameters: dict[str, Any]
    permission: str


@router.get("/tools", response_model=list[ToolInfo])
def list_tools(siris: SirisAI = Depends(get_siris)) -> list[ToolInfo]:
    """Every registered tool, including skills -- full detail (description,
    parameters, permission), not just the bare names `/siris/status` lists.
    Developer/UI introspection (milestone 22); unlike `available_tool_specs`
    this doesn't hide disabled RESTRICTED tools, since the point here is
    showing what's actually installed, not what the model is currently
    allowed to call."""
    return [ToolInfo(**entry) for entry in siris.list_tools()]


@router.get("/skills", response_model=list[ToolInfo])
def list_skills(siris: SirisAI = Depends(get_siris)) -> list[ToolInfo]:
    """The subset of `/siris/tools` that are skills -- a multi-step
    workflow over other tools (app/skills/base.py) rather than a single
    action."""
    return [ToolInfo(**entry) for entry in siris.list_skills()]


@router.get("/capabilities")
def capabilities(siris: SirisAI = Depends(get_siris)) -> dict[str, list[str]]:
    """Registered tool names grouped by category, e.g.
    {"docker": ["docker_list_containers", ...], "weather": [...]}. Coarser
    than `/siris/tools` -- "what can SirisAI do" at a glance, the shape
    milestone 31 (capability gap detection) will compare a request
    against."""
    return siris.capabilities()


class EntityRelationshipInfo(BaseModel):
    id: int
    subject_entity: str
    relation: str
    object_entity: str
    created_at: str


class SystemStateResponse(BaseModel):
    entities: list[str]
    relationships: list[EntityRelationshipInfo]


@router.get("/system/state", response_model=SystemStateResponse)
def system_state(siris: SirisAI = Depends(get_siris)) -> SystemStateResponse:
    """The full world model (milestone 21): every known entity and every
    relationship between them. To ask about one entity specifically, use
    the recall_entity_relationships tool (POST /siris/tools/{name}/run)
    instead of filtering this."""
    return SystemStateResponse(**siris.system_state())


class GoalObjectiveInfo(BaseModel):
    id: int
    description: str
    done: bool


class GoalInfo(BaseModel):
    id: int
    title: str
    status: str
    created_at: str
    updated_at: str
    objectives: list[GoalObjectiveInfo]


@router.get("/goals", response_model=list[GoalInfo])
def list_goals(status: str | None = None, siris: SirisAI = Depends(get_siris)) -> list[GoalInfo]:
    """Goals (milestone 23), most recently created first -- the direct-read
    equivalent of the list_goals tool, same relationship as
    GET /siris/events has to the recall_events tool."""
    return [GoalInfo(**g) for g in siris.goals.list_goals(status=status)]


class ChatRequest(BaseModel):
    prompt: str
    conversation_id: str | None = None
    use_tools: bool = True
    use_context: bool = True
    # Force a specific model router role (see app/llm/router.py), e.g.
    # "fast". Omit to let SIRISAI_MODEL_ROUTER_POLICY decide automatically.
    # An explicit role that isn't actually configured falls back to
    # "general" rather than erroring.
    model_role: str | None = None
    # Opt into the task planner (app/planning/planner.py, milestone 24):
    # lets this turn chain multiple tool calls (plan -> execute -> verify
    # -> ... -> respond) instead of at most one. False by default so
    # existing callers see no behavior change unless they ask for it.
    use_planner: bool = False
    max_plan_steps: int = 5


class ToolCallResult(BaseModel):
    name: str
    arguments: dict[str, Any]
    result: Any


class PendingToolCall(BaseModel):
    name: str
    arguments: dict[str, Any]


class ChatResponse(BaseModel):
    conversation_id: str
    response: str | None = None
    tool_call: ToolCallResult | None = None
    # Set instead of tool_call when the model picked a CONFIRM_REQUIRED tool
    # (see app/plugins/base.py's ToolPermission). Nothing ran yet -- POST
    # the same name/arguments to /siris/chat/confirm to actually run it.
    confirmation_required: PendingToolCall | None = None
    # Which model router role actually handled this turn (see
    # app/llm/router.py) -- "general" unless a "fast" model is configured
    # and either requested or selected by the routing policy.
    model_role: str = "general"
    # Set instead of tool_call when request.use_planner was true and at
    # least one step ran -- every tool call the planner made this turn, in
    # order (see app/planning/planner.py). Empty/absent for a plan that
    # produced a final answer without needing any tool.
    plan: list[ToolCallResult] | None = None


def _load_messages(siris: SirisAI, conversation_id: str) -> list[dict[str, Any]]:
    history = siris.conversation.get_messages(conversation_id)
    return [{"role": m["role"], "content": m["content"]} for m in history if m["role"] in ("user", "assistant")]


async def _execute_tool_call(
    siris: SirisAI,
    conversation_id: str,
    name: str,
    arguments: dict[str, Any],
    *,
    confirmed: bool,
    model_role: str = "general",
    messages: list[dict[str, Any]] | None = None,
    llm_client: LLMClient | None = None,
) -> ChatResponse:
    """Shared by POST /chat and POST /chat/confirm: run a tool, persist the
    exchange, and shape the response -- including the ConfirmationRequired
    case, so a client can display and then confirm a proposed action.

    When `messages`/`llm_client` are given, also feeds the tool result back
    to the model for a final natural-language answer (a second round-trip)
    instead of returning the raw result alone -- both `response` and
    `tool_call` are set on the returned `ChatResponse` so a client can show
    either or both. Falls back to the tool-call-only shape (`response` left
    unset) if no LLM client is available, or if the follow-up call fails or
    the model doesn't produce text -- callers should already treat a raw
    tool result as an acceptable response, this is a best-effort upgrade."""
    try:
        output = siris.run_tool(name, arguments, conversation_id=conversation_id, confirmed=confirmed)
    except ConfirmationRequired:
        return ChatResponse(
            conversation_id=conversation_id,
            confirmation_required=PendingToolCall(name=name, arguments=arguments),
            model_role=model_role,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from None
    except Exception:
        logger.exception("Tool '%s' failed", name)
        raise HTTPException(status_code=502, detail=f"Tool '{name}' failed.") from None

    siris.conversation.add_message(
        conversation_id,
        role="tool",
        content=json.dumps(output),
        tool_name=name,
        tool_arguments=arguments,
    )
    tool_call = ToolCallResult(name=name, arguments=arguments, result=output)

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
            # No `tools` offered here on purpose -- this call is meant to
            # produce a final text answer, not choose another tool.
            follow_up = await llm_client.generate(follow_up_messages)
        except Exception:
            logger.exception("Follow-up LLM call after tool '%s' failed", name)
        else:
            if follow_up.text:
                siris.conversation.add_message(conversation_id, role="assistant", content=follow_up.text)
                return ChatResponse(
                    conversation_id=conversation_id,
                    response=follow_up.text,
                    tool_call=tool_call,
                    model_role=model_role,
                )

    return ChatResponse(conversation_id=conversation_id, tool_call=tool_call, model_role=model_role)


async def _run_planned_chat(
    siris: SirisAI,
    conversation_id: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    llm_client: LLMClient,
    model_role: str,
    max_steps: int,
) -> ChatResponse:
    """The `request.use_planner=True` path of POST /chat: run the planner
    (app/planning/planner.py) instead of at most one tool call, persist
    every step it took, and shape the response the same way a
    ConfirmationRequired/failed/successful single tool call already is."""
    try:
        plan_result = await Planner(max_steps=max_steps).run(siris, conversation_id, messages, tools, llm_client)
    except Exception:
        logger.exception("Planner's LLM generate() failed")
        raise HTTPException(status_code=502, detail="LLM backend call failed.") from None

    for step in plan_result.steps:
        siris.conversation.add_message(
            conversation_id, role="tool", content=json.dumps(step.result), tool_name=step.name, tool_arguments=step.arguments
        )
    plan = [ToolCallResult(name=s.name, arguments=s.arguments, result=s.result) for s in plan_result.steps] or None

    if plan_result.pending_confirmation:
        name, arguments = plan_result.pending_confirmation
        return ChatResponse(
            conversation_id=conversation_id,
            plan=plan,
            confirmation_required=PendingToolCall(name=name, arguments=arguments),
            model_role=model_role,
        )

    if plan_result.error:
        status_code = 403 if plan_result.permission_denied else 502
        raise HTTPException(status_code=status_code, detail=plan_result.error)

    if plan_result.final_text:
        siris.conversation.add_message(conversation_id, role="assistant", content=plan_result.final_text)

    return ChatResponse(conversation_id=conversation_id, response=plan_result.final_text, plan=plan, model_role=model_role)


@router.post("/chat")
async def chat(request: ChatRequest, siris: SirisAI = Depends(get_siris)) -> ChatResponse:
    """Call the configured LLM backend with tools offered, carrying prior
    turns of `conversation_id` as context.

    Pass the `conversation_id` a previous call returned to continue that
    conversation; omit it (or pass an unknown id) to start a new one -- the
    response always carries the id in play, new or continued.

    If the model chooses to call a tool, SirisAI executes it, feeds the
    result back to the model for a final natural-language answer (a second
    round-trip -- `response` carries that text, `tool_call` the raw
    call+result for transparency), and falls back to `tool_call` alone if
    that follow-up call fails or the model doesn't produce text -- unless
    the tool requires confirmation, in which case `confirmation_required` is
    set instead and nothing has run yet; see POST /siris/chat/confirm. Tool
    exchanges are persisted to the conversation for inspection via
    GET /siris/conversations/{id}, but aren't replayed to the model as
    context on later turns yet -- only user/assistant text is.

    Unless `use_context` is false, a compact context package (current
    system status, recent events, memories relevant to the prompt -- see
    app/context/engine.py) is prepended as a system message on every call,
    recomputed fresh each time. It's not part of the persisted
    conversation history and doesn't hit the audit log -- it's
    background situational awareness, not a decision SirisAI made.

    Which model actually handles the turn is decided by the model router
    (app/llm/router.py, `SIRISAI_MODEL_ROUTER_POLICY`) unless `model_role`
    forces a specific one; the response's `model_role` says which one ran.

    Pass `use_planner=true` to let this turn chain multiple tool calls
    (app/planning/planner.py, milestone 24) instead of at most one --
    `plan` carries every call+result made, in order, in place of
    `tool_call`. False by default: nothing above changes unless you ask
    for it.
    """
    assert siris.llm is not None

    conversation_id = request.conversation_id
    if conversation_id is None or not siris.conversation.conversation_exists(conversation_id):
        conversation_id = siris.conversation.create_conversation()

    siris.conversation.add_message(conversation_id, role="user", content=request.prompt)

    messages = _load_messages(siris, conversation_id)

    if request.use_context:
        context_text = siris.context.build(siris, request.prompt)
        if context_text:
            system_message = {"role": "system", "content": f"Context (may or may not be relevant):\n{context_text}"}
            messages = [system_message, *messages]

    tools = siris.available_tool_specs() if request.use_tools else None

    assert siris.model_router is not None
    model_role, llm_client = siris.model_router.pick(siris.llm, request.prompt, role=request.model_role)

    if request.use_planner:
        return await _run_planned_chat(siris, conversation_id, messages, tools, llm_client, model_role, request.max_plan_steps)

    try:
        result = await llm_client.generate(messages, tools=tools)
    except Exception:
        logger.exception("LLM generate() failed")
        raise HTTPException(status_code=502, detail="LLM backend call failed.") from None

    if result.tool_calls:
        call = result.tool_calls[0]
        return await _execute_tool_call(
            siris,
            conversation_id,
            call.name,
            call.arguments,
            confirmed=False,
            model_role=model_role,
            messages=messages,
            llm_client=llm_client,
        )

    siris.conversation.add_message(conversation_id, role="assistant", content=result.text)
    return ChatResponse(conversation_id=conversation_id, response=result.text, model_role=model_role)


class ConfirmToolRequest(BaseModel):
    conversation_id: str
    tool_name: str
    arguments: dict[str, Any]


@router.post("/chat/confirm")
async def confirm_tool_call(request: ConfirmToolRequest, siris: SirisAI = Depends(get_siris)) -> ChatResponse:
    """Actually run a tool call a previous /siris/chat response flagged as
    `confirmation_required` -- re-submit the exact name/arguments it
    returned. There's no server-side pending-action state to look up by id;
    the client is trusted to relay what it was shown (this is a single-user
    service behind SIRISAI_API_KEY, not a multi-tenant approval queue)."""
    if not siris.conversation.conversation_exists(request.conversation_id):
        raise HTTPException(status_code=404, detail="Conversation not found.")
    messages = _load_messages(siris, request.conversation_id)
    return await _execute_tool_call(
        siris,
        request.conversation_id,
        request.tool_name,
        request.arguments,
        confirmed=True,
        messages=messages,
        llm_client=siris.llm,
    )


class MessageResponse(BaseModel):
    role: str
    content: str | None
    tool_name: str | None
    tool_arguments: dict[str, Any] | None
    created_at: str


@router.get("/conversations/{conversation_id}", response_model=list[MessageResponse])
def get_conversation(conversation_id: str, siris: SirisAI = Depends(get_siris)) -> list[MessageResponse]:
    if not siris.conversation.conversation_exists(conversation_id):
        raise HTTPException(status_code=404, detail="Conversation not found.")
    return [MessageResponse(**m) for m in siris.conversation.get_messages(conversation_id)]


class ActivityEntryResponse(BaseModel):
    id: int
    conversation_id: str | None
    tool_name: str
    arguments: dict[str, Any]
    result: Any
    error: str | None
    confirmation_required: bool
    duration_ms: float
    created_at: str


@router.get("/activity", response_model=list[ActivityEntryResponse])
def get_activity(limit: int = 50, siris: SirisAI = Depends(get_siris)) -> list[ActivityEntryResponse]:
    """Recent tool executions (successful or not), most recent first."""
    return [ActivityEntryResponse(**entry) for entry in siris.audit.list_recent(limit=limit)]


class RunToolRequest(BaseModel):
    arguments: dict[str, Any] = {}
    confirmed: bool = False


class RunToolResponse(BaseModel):
    tool_call: ToolCallResult | None = None
    confirmation_required: PendingToolCall | None = None


@router.post("/tools/{name}/run", response_model=RunToolResponse)
def run_tool_directly(name: str, request: RunToolRequest, siris: SirisAI = Depends(get_siris)) -> RunToolResponse:
    """Invoke any registered tool -- including a skill, which is just a
    tool whose implementation calls other tools -- directly, without going
    through the LLM. Outside of a chat turn, so nothing is added to a
    conversation transcript; the call is still recorded to the audit log
    (GET /siris/activity) like any other tool invocation."""
    if siris.tools.get(name) is None:
        raise HTTPException(status_code=404, detail=f"Unknown tool: {name}")

    try:
        output = siris.run_tool(name, request.arguments, confirmed=request.confirmed)
    except ConfirmationRequired:
        return RunToolResponse(confirmation_required=PendingToolCall(name=name, arguments=request.arguments))
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from None
    except Exception:
        logger.exception("Tool '%s' failed", name)
        raise HTTPException(status_code=502, detail=f"Tool '{name}' failed.") from None

    return RunToolResponse(tool_call=ToolCallResult(name=name, arguments=request.arguments, result=output))
