"""Routes for the SirisAI core service itself.

Kept separate from `health.py` on purpose: `/health` is an infra-level
liveness probe, `/siris/status` is app-level introspection (what's
registered, what backend is active) that will grow as the core does.
"""

import logging

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


class ChatResponse(BaseModel):
    response: str


@router.post("/chat")
async def chat(request: ChatRequest, siris: SirisAI = Depends(get_siris)) -> ChatResponse:
    """Single-turn passthrough to the configured LLM backend.

    No conversation history, tool use, or memory yet -- those are later
    milestones. This exists to prove the model-agnostic LLM interface works
    end-to-end from an actual endpoint.
    """
    assert siris.llm is not None
    try:
        text = await siris.llm.generate(request.prompt)
    except Exception:
        logger.exception("LLM generate() failed")
        raise HTTPException(status_code=502, detail="LLM backend call failed.") from None

    return ChatResponse(response=text)
