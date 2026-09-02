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


class ChatResponse(BaseModel):
    conversation_id: str
    response: str | None = None
    tool_call: ToolCallResult | None = None


@router.post("/chat")
async def chat(request: ChatRequest, siris: SirisAI = Depends(get_siris)) -> ChatResponse:
    """Call the configured LLM backend with tools offered, carrying prior
    turns of `conversation_id` as context.

    Pass the `conversation_id` a previous call returned to continue that
    conversation; omit it (or pass an unknown id) to start a new one -- the
    response always carries the id in play, new or continued.

    If the model chooses to call a tool, SirisAI executes it and returns the
    result directly (no second round-trip to the model to summarize it yet
    -- that's a natural next step, not this milestone). Tool exchanges are
    still persisted to the conversation for inspection via
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

    tools = siris.tools.specs() if request.use_tools else None

    try:
        result = await siris.llm.generate(messages, tools=tools)
    except Exception:
        logger.exception("LLM generate() failed")
        raise HTTPException(status_code=502, detail="LLM backend call failed.") from None

    if result.tool_calls:
        call = result.tool_calls[0]
        try:
            output = siris.run_tool(call.name, call.arguments)
        except Exception:
            logger.exception("Tool '%s' failed", call.name)
            raise HTTPException(status_code=502, detail=f"Tool '{call.name}' failed.") from None

        siris.conversation.add_message(
            conversation_id,
            role="tool",
            content=json.dumps(output),
            tool_name=call.name,
            tool_arguments=call.arguments,
        )
        return ChatResponse(
            conversation_id=conversation_id,
            tool_call=ToolCallResult(name=call.name, arguments=call.arguments, result=output),
        )

    siris.conversation.add_message(conversation_id, role="assistant", content=result.text)
    return ChatResponse(conversation_id=conversation_id, response=result.text)


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
