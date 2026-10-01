"""The HUD's data: one call with everything a wall-tablet dashboard shows
(GET /siris/hud/summary), and the autonomy timeline
(GET /siris/autonomy/timeline). Every section is best-effort -- a down
integration blanks its own tile, never the page."""

import asyncio
import logging
from typing import Any, Callable

from fastapi import APIRouter, Depends

from app.audit.autonomy import timeline
from app.core import clock, guest
from app.core.auth import require_api_key
from app.core.siris import SirisAI, get_siris
from app.core.user_context import resolve_user_id

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/siris", tags=["hud"], dependencies=[Depends(require_api_key), Depends(resolve_user_id)])


def _try(fn: Callable[[], Any]) -> Any:
    try:
        return fn()
    except Exception:
        logger.debug("HUD section failed", exc_info=True)
        return None


def _summary(siris: SirisAI) -> dict[str, Any]:
    from app.energy.service import status as energy_status

    def tool(name: str, **args: Any) -> Any:
        found = siris.tools.get(name)
        return found.run(**args) if found is not None else None

    now = clock.now_local(siris.settings.timezone)
    return {
        "time": now.isoformat(),
        "system": _try(lambda: tool("system_info")),
        "weather": _try(lambda: tool("weather_current")),
        "next_events": _try(lambda: (tool("calendar_upcoming_events", days_ahead=1) or [])[:3]),
        "guest_mode": _try(lambda: guest.status(siris)),
        "energy": _try(lambda: energy_status(siris)),
        "parcels": _try(lambda: siris.parcels.active()),
        "car": _try(lambda: tool("car_status")),
        "cameras": _try(lambda: siris._frigate_client.list_cameras() if siris._frigate_client else []),
        "autonomy": _try(lambda: timeline(siris, hours=24)["items"][:8]),
        "recent": _try(lambda: [
            {"tool": a["tool_name"], "time": a["created_at"], "error": bool(a["error"])} for a in siris.audit.list_recent(limit=6)
        ]),
    }


@router.get("/hud/summary")
async def hud_summary(siris: SirisAI = Depends(get_siris)) -> dict[str, Any]:
    return await asyncio.to_thread(_summary, siris)


@router.get("/autonomy/timeline")
def autonomy_timeline(hours: float = 24, include_reads: bool = False, siris: SirisAI = Depends(get_siris)) -> dict[str, Any]:
    return timeline(siris, hours=max(1, min(hours, 24 * 14)), include_reads=include_reads)
