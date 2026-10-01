"""Tool-choice evals over HTTP (app/evals/tool_choice.py)."""

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from app.core.auth import require_api_key
from app.core.siris import SirisAI, get_siris
from app.core.user_context import resolve_user_id
from app.evals import tool_choice

router = APIRouter(prefix="/siris/evals", tags=["evals"], dependencies=[Depends(require_api_key), Depends(resolve_user_id)])


@router.post("/run")
async def run_evals(selection_only: bool = False, siris: SirisAI = Depends(get_siris)) -> dict[str, Any]:
    """Selection (fast, no model) and -- unless selection_only -- the real
    model's tool choice for every case. The full run makes one model call per
    case, so it takes a while on a home GPU."""
    if selection_only:
        return {"selection": await asyncio.to_thread(tool_choice.selection_report, siris)}
    if siris.settings.llm_provider == "none":
        raise HTTPException(status_code=501, detail="No LLM backend configured (SIRISAI_LLM_PROVIDER).")
    return await asyncio.to_thread(tool_choice.run_and_record, siris, False)


@router.get("/history")
def eval_history(siris: SirisAI = Depends(get_siris)) -> list[dict[str, Any]]:
    return siris.state.get(tool_choice.HISTORY_KEY) or []
