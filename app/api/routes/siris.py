"""Routes for the SirisAI core service itself.

Kept separate from `health.py` on purpose: `/health` is an infra-level
liveness probe, `/siris/status` is app-level introspection (what's
registered, what backend is active) that will grow as the core does.
"""

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.core.siris import SirisAI, get_siris

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/siris", tags=["siris"])


@router.get("/status")
def status(siris: SirisAI = Depends(get_siris)) -> dict[str, object]:
    return siris.status()


class ChatRequest(BaseModel):
    prompt: str
    use_tools: bool = True


class ToolCallResult(BaseModel):
    name: str
    arguments: dict[str, Any]
    result: Any


class ChatResponse(BaseModel):
    response: str | None = None
    tool_call: ToolCallResult | None = None


@router.post("/chat")
async def chat(request: ChatRequest, siris: SirisAI = Depends(get_siris)) -> ChatResponse:
    """Single-turn call to the configured LLM backend, with tools offered.

    If the model chooses to call a tool, SirisAI executes it and returns the
    result directly (no second round-trip to the model to summarize it yet
    -- that's a natural next step, not this milestone). Otherwise the
    model's text answer is returned as-is.
    """
    assert siris.llm is not None
    tools = siris.tools.specs() if request.use_tools else None

    try:
        result = await siris.llm.generate(request.prompt, tools=tools)
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

        return ChatResponse(tool_call=ToolCallResult(name=call.name, arguments=call.arguments, result=output))

    return ChatResponse(response=result.text)
