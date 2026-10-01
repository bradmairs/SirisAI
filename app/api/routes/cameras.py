"""Camera frames, proxied from Frigate so the web UI and HUD can show them
through SirisAI's own auth instead of exposing Frigate directly."""

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response

from app.core.auth import require_api_key
from app.core.siris import SirisAI, get_siris
from app.core.user_context import resolve_user_id

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/siris/cameras", tags=["cameras"], dependencies=[Depends(require_api_key), Depends(resolve_user_id)])


@router.get("")
def list_cameras(siris: SirisAI = Depends(get_siris)) -> list[dict]:
    """Frigate's cameras, or [] when Frigate isn't configured."""
    if siris._frigate_client is None:
        return []
    return siris._frigate_client.list_cameras()


@router.get("/{camera}/latest.jpg")
async def latest_frame(camera: str, height: int = 480, siris: SirisAI = Depends(get_siris)) -> Response:
    client = siris._frigate_client
    if client is None:
        raise HTTPException(status_code=404, detail="Frigate isn't configured (SIRISAI_FRIGATE_BASE_URL).")
    try:
        image = await asyncio.to_thread(client.latest_snapshot, camera, max(120, min(height, 1080)))
    except Exception:
        logger.exception("Fetching a frame from camera %s failed", camera)
        raise HTTPException(status_code=502, detail=f"Couldn't get a frame from {camera}.") from None
    return Response(content=image, media_type="image/jpeg", headers={"Cache-Control": "no-store"})
