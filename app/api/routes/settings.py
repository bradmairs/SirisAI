"""The web UI's Settings page (app/core/settings_overrides.py).

    GET /siris/settings      the editable settings, grouped, with their values
    PUT /siris/settings      {"changes": {"server_watch_minutes": 10}, "pin": "..."}

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


@router.put("")
def update_settings(body: SettingsUpdate, siris: SirisAI = Depends(get_siris)) -> dict[str, Any]:
    if guest.is_on(siris):
        raise HTTPException(status_code=403, detail="Settings can't be changed in guest mode.")
    if not body.changes:
        raise HTTPException(status_code=422, detail="Nothing to change.")
    if approvals.pin_enabled(siris):
        if not body.pin:
            raise HTTPException(status_code=403, detail="pin_required: changing settings needs your PIN")
        try:
            approvals.check_pin(siris, body.pin)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from None
    try:
        result = settings_overrides.update(siris, body.changes)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return {**result, "settings": settings_overrides.describe(siris.settings)}
