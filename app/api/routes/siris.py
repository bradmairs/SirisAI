"""Routes for the SirisAI core service itself.

Kept separate from `health.py` on purpose: `/health` is an infra-level
liveness probe, `/siris/status` is app-level introspection (what's
registered, what backend is active) that will grow as the core does.
"""

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.core.auth import require_api_key
from app.core.siris import SirisAI, get_siris
from app.core.user_context import resolve_user_id
from app.cortex.orchestrator import Cortex, CortexResult, ToolCallOutcome
from app.plugins.base import ConfirmationRequired

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/siris",
    tags=["siris"],
    dependencies=[Depends(require_api_key), Depends(resolve_user_id)],
)


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
def list_goals(
    status: str | None = None, siris: SirisAI = Depends(get_siris)
) -> list[GoalInfo]:
    """Goals (milestone 23), most recently created first -- the direct-read
    equivalent of the list_goals tool, same relationship as
    GET /siris/events has to the recall_events tool."""
    return [GoalInfo(**g) for g in siris.goals.list_goals(status=status)]


class CapabilityGapInfo(BaseModel):
    id: int
    requested_capability: str
    suggested_integration: str | None
    suggested_tool_spec: dict[str, Any] | None
    created_at: str


@router.get("/capability_gaps", response_model=list[CapabilityGapInfo])
def list_capability_gaps(
    limit: int = 50, siris: SirisAI = Depends(get_siris)
) -> list[CapabilityGapInfo]:
    """Every "SirisAI can't do this yet" the model has reported (milestone
    31, report_capability_gap tool), most recent first -- a development
    backlog someone (or Claude Code, working this same repo) can act on."""
    return [
        CapabilityGapInfo(**g) for g in siris.capability_gaps.list_gaps(limit=limit)
    ]


class LLMTokenSpendResponse(BaseModel):
    total_prompt_tokens: float
    total_completion_tokens: float
    total_tokens: float
    call_count: int


class PowerDrawResponse(BaseModel):
    latest_watts: float | None
    average_watts: float | None
    sample_count: int


class MetricsDashboardResponse(BaseModel):
    llm_tokens: LLMTokenSpendResponse
    # None entirely when SIRISAI_POWER_SENSOR_ENTITY_ID isn't configured
    # -- distinct from a configured sensor with zero samples yet
    # (sample_count: 0, latest/average: null), same "empty = feature not
    # engaged" convention as everywhere else optional in this codebase.
    power_draw: PowerDrawResponse | None


@router.get("/metrics/dashboard", response_model=MetricsDashboardResponse)
def metrics_dashboard(siris: SirisAI = Depends(get_siris)) -> MetricsDashboardResponse:
    """Cost/resource dashboard (milestone 47): LLM token spend
    (app/llm/metering.py, tracked automatically once an LLM backend is
    configured) and, if `SIRISAI_POWER_SENSOR_ENTITY_ID` is set, homelab
    power draw read from Home Assistant by server_health_check -- both
    direct reuse of MetricsStore's existing sample/baseline machinery
    (milestone 27), not a new store. Server-wide, not per-user (milestone
    46): infrastructure/cost data, not personal preference."""
    llm_tokens = LLMTokenSpendResponse(
        total_prompt_tokens=siris.metrics.total("llm_tokens_prompt"),
        total_completion_tokens=siris.metrics.total("llm_tokens_completion"),
        total_tokens=siris.metrics.total("llm_tokens_total"),
        call_count=siris.metrics.sample_count("llm_tokens_total"),
    )

    power_draw = None
    if siris.settings.power_sensor_entity_id:
        baseline = siris.metrics.baseline("power_draw_watts", min_samples=1)
        power_draw = PowerDrawResponse(
            latest_watts=siris.metrics.latest_value("power_draw_watts"),
            average_watts=baseline.mean if baseline else None,
            sample_count=siris.metrics.sample_count("power_draw_watts"),
        )

    return MetricsDashboardResponse(llm_tokens=llm_tokens, power_draw=power_draw)


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
    # A satellite's room (milestone 48, see app/satellites/service.py) --
    # when set, this counts as fresh activity in that room, so
    # SatelliteStore.current_room() (what speak_in_current_room targets)
    # reflects it. Omitted by non-satellite clients (the web UI, direct
    # API calls); nothing about the rest of this endpoint changes either
    # way.
    room: str | None = None


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
    # Set instead of tool_call when request.use_planner was true (on
    # /siris/chat) or use_planner was true on a /siris/chat/confirm that
    # continued a plan past a confirmed step, and at least one call ran --
    # every tool call made this turn, in order (see
    # app/planning/planner.py). Empty/absent for a plan that produced a
    # final answer without needing any tool.
    plan: list[ToolCallResult] | None = None


def _to_tool_call_result(outcome: ToolCallOutcome) -> ToolCallResult:
    return ToolCallResult(
        name=outcome.name, arguments=outcome.arguments, result=outcome.result
    )


def _shape_response(result: CortexResult) -> ChatResponse:
    """Turn a Cortex result -- plain dataclasses, no HTTP awareness -- into
    the ChatResponse shape POST /chat and /chat/confirm both return,
    raising the appropriate HTTPException for the `error` case. This is the
    entire route/orchestration boundary: everything upstream of this
    function is Cortex's job, everything downstream is HTTP shaping."""
    if result.error:
        status_code = 403 if result.permission_denied else 502
        raise HTTPException(status_code=status_code, detail=result.error)

    return ChatResponse(
        conversation_id=result.conversation_id,
        response=result.response,
        tool_call=_to_tool_call_result(result.tool_call) if result.tool_call else None,
        confirmation_required=PendingToolCall(
            name=result.pending_confirmation[0],
            arguments=result.pending_confirmation[1],
        )
        if result.pending_confirmation
        else None,
        model_role=result.model_role,
        plan=[_to_tool_call_result(step) for step in result.plan] or None,
    )


@router.post("/chat")
async def chat(
    request: ChatRequest, siris: SirisAI = Depends(get_siris)
) -> ChatResponse:
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
    exchanges are persisted to the conversation (GET /siris/conversations/{id}
    for inspection) and replayed to the model as real chat history on later
    turns too (Cortex._load_messages), not just whatever text it produced
    about them -- so a later turn can reference a prior tool result
    directly.

    Unless `use_context` is false, a compact context package (current
    system status, recent events, memories relevant to the prompt -- see
    app/context/engine.py) is prepended as a system message on every call,
    recomputed fresh each time. It's not part of the persisted
    conversation history and doesn't hit the audit log -- it's
    background situational awareness, not a decision SirisAI made.

    If `SIRISAI_PERSONA` is set, it's also prepended as a system message
    on every call regardless of `use_context` -- tone/personality, a
    separate concern from situational context (see Cortex's own
    docstring, milestone 45). Unset by default, same raw-model behavior
    as before this existed.

    Which model actually handles the turn is decided by the model router
    (app/llm/router.py, `SIRISAI_MODEL_ROUTER_POLICY`) unless `model_role`
    forces a specific one; the response's `model_role` says which one ran.

    Pass `use_planner=true` to let this turn chain multiple tool calls
    (app/planning/planner.py, milestone 24) instead of at most one --
    `plan` carries every call+result made, in order, in place of
    `tool_call`. False by default: nothing above changes unless you ask
    for it.

    A satellite (milestone 48) passes its own `room` on every call --
    this counts as fresh activity in that room, so a later
    speak_in_current_room call (or anything else consulting
    SatelliteStore.current_room()) can push a reply to wherever the
    user most recently actually spoke from. Non-satellite clients omit
    it; nothing else here changes either way.

    Conversation handoff between rooms (milestone 75): a satellite's own
    conversation_id lives only in that satellite process's memory
    (satellite/siris_satellite/client.py), so the *first* request from a
    different room than the one you were just talking in always arrives
    with no conversation_id -- without this, that would silently start a
    brand-new conversation and lose whatever was just discussed. When a
    request carries `room` but no usable conversation_id, and some
    *other* room had activity within SIRISAI_SATELLITE_HANDOFF_WINDOW_SECONDS,
    that room's last conversation is resumed instead of starting fresh.
    Non-satellite clients (no `room`) are unaffected -- this only
    triggers for the exact case it's meant to fix.
    """
    assert siris.llm is not None

    conversation_id = request.conversation_id
    if conversation_id is None or not siris.conversation.conversation_exists(
        conversation_id
    ):
        handoff_id = None
        if request.room:
            handoff_id = siris.satellites.handoff_candidate(
                exclude_room=request.room,
                within_seconds=siris.settings.satellite_handoff_window_seconds,
            )
        conversation_id = handoff_id or siris.conversation.create_conversation()

    if request.room:
        siris.satellites.record_activity(request.room, conversation_id=conversation_id)

    result = await Cortex(max_plan_steps=request.max_plan_steps).process(
        siris,
        conversation_id,
        request.prompt,
        use_context=request.use_context,
        use_tools=request.use_tools,
        use_planner=request.use_planner,
        model_role=request.model_role,
    )
    return _shape_response(result)


class ConfirmToolRequest(BaseModel):
    conversation_id: str
    tool_name: str
    arguments: dict[str, Any]
    # Set true when this confirmation came from a use_planner=true turn
    # that stopped mid-plan (the response that carried `confirmation_required`
    # also carried `plan`, not `tool_call`) -- continues the rest of the
    # plan after the confirmed step runs, instead of ending the turn there.
    # False by default: confirming a single non-planned tool call behaves
    # exactly as before.
    use_planner: bool = False
    max_plan_steps: int = 5


@router.post("/chat/confirm")
async def confirm_tool_call(
    request: ConfirmToolRequest, siris: SirisAI = Depends(get_siris)
) -> ChatResponse:
    """Actually run a tool call a previous /siris/chat response flagged as
    `confirmation_required` -- re-submit the exact name/arguments it
    returned. There's no server-side pending-action state to look up by id;
    the client is trusted to relay what it was shown (this is a single-user
    service behind SIRISAI_API_KEY, not a multi-tenant approval queue).

    Pass `use_planner: true` if the original response also carried `plan`
    (i.e. it came from a use_planner=true turn that stopped mid-plan for
    this confirmation) to continue the rest of the plan afterward instead
    of ending the turn after just this one step -- see Cortex.confirm()."""
    if not siris.conversation.conversation_exists(request.conversation_id):
        raise HTTPException(status_code=404, detail="Conversation not found.")
    result = await Cortex(max_plan_steps=request.max_plan_steps).confirm(
        siris,
        request.conversation_id,
        request.tool_name,
        request.arguments,
        use_planner=request.use_planner,
    )
    return _shape_response(result)


class MessageResponse(BaseModel):
    role: str
    content: str | None
    tool_name: str | None
    tool_arguments: dict[str, Any] | None
    created_at: str


@router.get("/conversations/{conversation_id}", response_model=list[MessageResponse])
def get_conversation(
    conversation_id: str, siris: SirisAI = Depends(get_siris)
) -> list[MessageResponse]:
    if not siris.conversation.conversation_exists(conversation_id):
        raise HTTPException(status_code=404, detail="Conversation not found.")
    return [
        MessageResponse(**m) for m in siris.conversation.get_messages(conversation_id)
    ]


class ActivityEntryResponse(BaseModel):
    id: int
    conversation_id: str | None
    tool_name: str
    arguments: dict[str, Any]
    result: Any
    error: str | None
    confirmation_required: bool
    duration_ms: float
    # Explainable autonomy (milestone 28) -- all three are None for calls
    # that predate this milestone, or that had nothing meaningful to
    # record (most plain READ lookups).
    trigger: str | None
    evidence: Any
    policy: str | None
    # Deterministic per-tool verification (a hardening pass) -- both None
    # unless the tool that ran defines Tool.verify() (see
    # app/plugins/builtin/docker.py's DockerRestartContainerTool for the
    # reference case); most tools don't, same as trigger/evidence/policy
    # being None for calls with nothing to say there.
    verified: bool | None
    verification_detail: str | None
    created_at: str


@router.get("/activity", response_model=list[ActivityEntryResponse])
def get_activity(
    limit: int = 50, siris: SirisAI = Depends(get_siris)
) -> list[ActivityEntryResponse]:
    """Recent tool executions (successful or not), most recent first."""
    return [
        ActivityEntryResponse(**entry) for entry in siris.audit.list_recent(limit=limit)
    ]


class RunToolRequest(BaseModel):
    arguments: dict[str, Any] = {}
    confirmed: bool = False
    # Report what the tool would do instead of actually doing it
    # (milestone 25, see Tool.dry_run()) -- ignores `confirmed`, since a
    # dry run has no effect to confirm. Not every tool supports this;
    # unsupported returns 501.
    dry_run: bool = False


class RunToolResponse(BaseModel):
    tool_call: ToolCallResult | None = None
    confirmation_required: PendingToolCall | None = None


@router.post("/tools/{name}/run", response_model=RunToolResponse)
def run_tool_directly(
    name: str, request: RunToolRequest, siris: SirisAI = Depends(get_siris)
) -> RunToolResponse:
    """Invoke any registered tool -- including a skill, which is just a
    tool whose implementation calls other tools -- directly, without going
    through the LLM. Outside of a chat turn, so nothing is added to a
    conversation transcript; the call is still recorded to the audit log
    (GET /siris/activity) like any other tool invocation.

    Pass `dry_run: true` to preview instead of act -- bypasses
    SirisAI.run_tool() entirely (no permission gate, no audit entry: a
    dry run has no effect, so there's nothing to gate or audit), calling
    the tool's own `dry_run()` directly. 501 if the tool doesn't
    implement one."""
    tool = siris.tools.get(name)
    if tool is None:
        raise HTTPException(status_code=404, detail=f"Unknown tool: {name}")

    if request.dry_run:
        try:
            output = tool.dry_run(**request.arguments)
        except NotImplementedError:
            raise HTTPException(
                status_code=501, detail=f"Tool '{name}' does not support dry_run."
            ) from None
        except Exception:
            logger.exception("Tool '%s' dry_run failed", name)
            raise HTTPException(
                status_code=502, detail=f"Tool '{name}' dry_run failed."
            ) from None
        return RunToolResponse(
            tool_call=ToolCallResult(
                name=name, arguments=request.arguments, result=output
            )
        )

    try:
        output = siris.run_tool(
            name,
            request.arguments,
            confirmed=request.confirmed,
            trigger="direct API request: a human explicitly asked for exactly this tool",
        )
    except ConfirmationRequired:
        return RunToolResponse(
            confirmation_required=PendingToolCall(
                name=name, arguments=request.arguments
            )
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from None
    except Exception:
        logger.exception("Tool '%s' failed", name)
        raise HTTPException(status_code=502, detail=f"Tool '{name}' failed.") from None

    return RunToolResponse(
        tool_call=ToolCallResult(name=name, arguments=request.arguments, result=output)
    )
