"""Upkeep over HTTP (app/upkeep/): backups and the restore drill,
housekeeping, self-update, model trials."""

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.core.auth import require_api_key
from app.core.siris import SirisAI, get_siris
from app.core.user_context import resolve_user_id
from app.upkeep import backups, housekeeping, issues, models, updates

router = APIRouter(prefix="/siris/upkeep", tags=["upkeep"], dependencies=[Depends(require_api_key), Depends(resolve_user_id)])


class ModelRequest(BaseModel):
    model: str | None = None


class UpdateRequest(BaseModel):
    version: str | None = None


@router.get("")
def overview(siris: SirisAI = Depends(get_siris)) -> dict[str, Any]:
    """Backups, the last housekeeping reading, update state, the model in use."""
    history = siris.state.get(housekeeping.HISTORY_KEY) or []
    return {
        "backups": backups.list_backups(siris.settings)[:5],
        "backup_warning": backups.freshness(siris),
        "housekeeping": history[-1] if history else None,
        "update": updates.status(siris),
        "model": models.current_model(siris),
        "model_trials": (siris.state.get(models.TRIALS_KEY) or [])[-5:],
        "github_issues": {"repo": siris.settings.github_issues_repo or None, "mode": siris.settings.github_issues_mode, "configured": issues.configured(siris)},
    }


@router.post("/backup")
async def backup_now(siris: SirisAI = Depends(get_siris)) -> dict[str, Any]:
    return await asyncio.to_thread(backups.make_backup, siris)


@router.post("/restore-drill")
async def restore_drill_now(siris: SirisAI = Depends(get_siris)) -> dict[str, Any]:
    return await asyncio.to_thread(backups.restore_drill, siris)


@router.post("/housekeeping")
async def housekeeping_now(siris: SirisAI = Depends(get_siris)) -> dict[str, Any]:
    return await asyncio.to_thread(housekeeping.run, siris)


@router.get("/update")
def update_status(siris: SirisAI = Depends(get_siris)) -> dict[str, Any]:
    return updates.status(siris)


@router.post("/update/check")
async def update_check(siris: SirisAI = Depends(get_siris)) -> dict[str, Any]:
    try:
        return await asyncio.to_thread(updates.check, siris)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Couldn't check GitHub: {str(exc)[:300]}") from exc


@router.post("/update/apply")
async def update_apply(body: UpdateRequest, siris: SirisAI = Depends(get_siris)) -> dict[str, Any]:
    """Ask the updater (deploy/updater.sh) for a version; the latest release when none is given."""
    version = body.version
    if not version:
        try:
            version = (await asyncio.to_thread(updates.latest_release, siris))["version"]
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Couldn't check GitHub: {str(exc)[:300]}") from exc
    try:
        return await asyncio.to_thread(updates.request, siris, version, "asked over the API")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/models/trial")
async def model_trial(body: ModelRequest, siris: SirisAI = Depends(get_siris)) -> dict[str, Any]:
    if not body.model:
        raise HTTPException(status_code=422, detail="Name the model to try")
    try:
        return await asyncio.to_thread(models.trial, siris, body.model)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/models/switch")
def model_switch(body: ModelRequest, siris: SirisAI = Depends(get_siris)) -> dict[str, Any]:
    """Use a model from now on; no model (or SIRISAI_OLLAMA_MODEL's) reverts."""
    return models.switch(siris, body.model)
