"""Voice satellite registry endpoints (milestone 48) -- how a satellite
tells SirisAI which room it's in and how to reach it, so SirisAI can
push a reply there later (see app/satellites/service.py and
speak_in_current_room, app/plugins/builtin/satellites.py). Global, not
per-user (milestone 46): satellites are physical infrastructure, not
personal data.
"""

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.core.auth import require_api_key
from app.core.siris import SirisAI, get_siris
from app.core.user_context import resolve_user_id

router = APIRouter(
    prefix="/siris/satellites", tags=["satellites"], dependencies=[Depends(require_api_key), Depends(resolve_user_id)]
)


class SatelliteRegisterRequest(BaseModel):
    room: str
    callback_url: str


class SatelliteResponse(BaseModel):
    id: int
    room: str
    callback_url: str
    last_active_at: str


@router.post("/register", response_model=SatelliteResponse)
def register_satellite(request: SatelliteRegisterRequest, siris: SirisAI = Depends(get_siris)) -> SatelliteResponse:
    """A satellite calls this on startup (see
    satellite/siris_satellite/__main__.py) with the room it's in and a
    base URL SirisAI can reach it at -- re-registering the same room
    updates callback_url/last_active_at rather than creating a
    duplicate (a restarted satellite process, or a changed port)."""
    return SatelliteResponse(**siris.satellites.register(request.room, request.callback_url))


@router.get("", response_model=list[SatelliteResponse])
def list_satellites(siris: SirisAI = Depends(get_siris)) -> list[SatelliteResponse]:
    return [SatelliteResponse(**s) for s in siris.satellites.list_satellites()]
