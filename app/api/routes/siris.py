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
from app.plugins.base import ConfirmationRequired

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/siris", tags=["siris"], dependencies=[Depends(require_api_key)])


@router.get("/status")
def status(siris: SirisAI = Depends(get_siris)) -> dict[str, object]:
    return siris.status()


class ChatRequest(BaseModel):
    prompt: str
    conversation_id: str | None = None
    use_tools: bool = True


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


def _execute_tool_call(
    siris: SirisAI, conversation_id: str, name: str, arguments: dict[str, Any], *, confirmed: bool
) -> ChatResponse:
    """Shared by POST /chat and POST /chat/confirm: run a tool, persist the
    exchange, and shape the response -- including the ConfirmationRequired
    case, so a client can display and then confirm a proposed action."""
    try:
        output = siris.run_tool(name, arguments, conversation_id=conversation_id, confirmed=confirmed)
    except ConfirmationRequired:
        return ChatResponse(
            conversation_id=conversation_id,
            confirmation_required=PendingToolCall(name=name, arguments=arguments),
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
    return ChatResponse(
        conversation_id=conversation_id,
        tool_call=ToolCallResult(name=name, arguments=arguments, result=output),
    )


@router.post("/chat")
async def chat(request: ChatRequest, siris: SirisAI = Depends(get_siris)) -> ChatResponse:
    """Call the configured LLM backend with tools offered, carrying prior
    turns of `conversation_id` as context.

    Pass the `conversation_id` a previous call returned to continue that
    conversation; omit it (or pass an unknown id) to start a new one -- the
    response always carries the id in play, new or continued.

    If the model chooses to call a tool, SirisAI executes it and returns the
    result directly (no second round-trip to the model to summarize it yet
    -- that's a natural next step, not this milestone) -- unless the tool
    requires confirmation, in which case `confirmation_required` is set
    instead and nothing has run yet; see POST /siris/chat/confirm. Tool
    exchanges are persisted to the conversation for inspection via
    GET /siris/conversations/{id}, but aren't replayed to the model as
    context on later turns yet -- only user/assistant text is.
    """
    assert siris.llm is not None

    conversation_id = request.conversation_id
    if conversation_id is None or not siris.conversation.conversation_exists(conversation_id):
        conversation_id = siris.conversation.create_conversation()

    siris.conversation.add_message(conversation_id, role="user", content=request.prompt)

    history = siris.conversation.get_messages(conversation_id)
    messages = [{"role": m["role"], "content": m["content"]} for m in history if m["role"] in ("user", "assistant")]

    tools = siris.available_tool_specs() if request.use_tools else None

    try:
        result = await siris.llm.generate(messages, tools=tools)
    except Exception:
        logger.exception("LLM generate() failed")
        raise HTTPException(status_code=502, detail="LLM backend call failed.") from None

    if result.tool_calls:
        call = result.tool_calls[0]
        return _execute_tool_call(siris, conversation_id, call.name, call.arguments, confirmed=False)

    siris.conversation.add_message(conversation_id, role="assistant", content=result.text)
    return ChatResponse(conversation_id=conversation_id, response=result.text)


class ConfirmToolRequest(BaseModel):
    conversation_id: str
    tool_name: str
    arguments: dict[str, Any]


@router.post("/chat/confirm")
def confirm_tool_call(request: ConfirmToolRequest, siris: SirisAI = Depends(get_siris)) -> ChatResponse:
    """Actually run a tool call a previous /siris/chat response flagged as
    `confirmation_required` -- re-submit the exact name/arguments it
    returned. There's no server-side pending-action state to look up by id;
    the client is trusted to relay what it was shown (this is a single-user
    service behind SIRISAI_API_KEY, not a multi-tenant approval queue)."""
    if not siris.conversation.conversation_exists(request.conversation_id):
        raise HTTPException(status_code=404, detail="Conversation not found.")
    return _execute_tool_call(siris, request.conversation_id, request.tool_name, request.arguments, confirmed=True)


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
