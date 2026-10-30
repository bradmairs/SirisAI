"""The web UI's Settings page (app/core/settings_overrides.py).

    GET /siris/settings      the editable settings, grouped, with their values
    PUT /siris/settings      {"changes": {"server_watch_minutes": 10}, "pin": "..."}
    GET /siris/settings/history          the last 50 changes, newest first
    POST /siris/settings/undo/{id}       {"pin": "..."}: put back what that change replaced

Saving needs the approval PIN when one is set (it can turn on reboots), and
is refused in guest mode. Secrets aren't editable here; they stay in .env.
"""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.core import approvals, guest, settings_overrides
from app.core.auth import require_api_key
from app.core.siris import SirisAI, get_siris
from app.core.user_context import resolve_user_id

router = APIRouter(prefix="/siris/settings", tags=["settings"], dependencies=[Depends(require_api_key), Depends(resolve_user_id)])


class SettingsUpdate(BaseModel):
    changes: dict[str, Any] = Field(default_factory=dict, description="Field name -> new value; null goes back to .env or the default.")
    pin: str | None = Field(None, max_length=32)


@router.get("")
def get_settings_page(siris: SirisAI = Depends(get_siris)) -> dict[str, Any]:
    return {
        "settings": settings_overrides.describe(siris.settings),
        "pin_enabled": approvals.pin_enabled(siris),
        "file": str(settings_overrides.path()),
    }


def _guard(siris: SirisAI, pin: str | None) -> None:
    if guest.is_on(siris):
        raise HTTPException(status_code=403, detail="Settings can't be changed in guest mode.")
    if approvals.pin_enabled(siris):
        if not pin:
            raise HTTPException(status_code=403, detail="pin_required: changing settings needs your PIN")
        try:
            approvals.check_pin(siris, pin)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from None


class UndoRequest(BaseModel):
    pin: str | None = Field(None, max_length=32)


@router.get("/history")
def settings_history(siris: SirisAI = Depends(get_siris)) -> dict[str, Any]:
    labels = {name: spec[1] for name, spec in settings_overrides.EDITABLE.items()}
    return {"changes": [{**h, "labels": {n: labels.get(n, n) for n in h["changes"]}} for h in settings_overrides.history(siris)]}


@router.post("/undo/{change_id}")
def undo_settings(change_id: int, body: UndoRequest | None = None, siris: SirisAI = Depends(get_siris)) -> dict[str, Any]:
    _guard(siris, body.pin if body else None)
    try:
        result = settings_overrides.undo(siris, change_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return {**result, "settings": settings_overrides.describe(siris.settings)}


@router.put("")
def update_settings(body: SettingsUpdate, siris: SirisAI = Depends(get_siris)) -> dict[str, Any]:
    if not body.changes:
        raise HTTPException(status_code=422, detail="Nothing to change.")
    _guard(siris, body.pin)
    try:
        result = settings_overrides.update(siris, body.changes)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return {**result, "settings": settings_overrides.describe(siris.settings)}
