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
    # Guest mode: a wall tablet may be in front of visitors, so the personal
    # tiles (calendar, parcels, car, what the brain learned) are left out
    # without even being read -- as the hub contract's brief does.
    private = guest.is_on(siris)

    def personal(fn: Callable[[], Any]) -> Any:
        return None if private else _try(fn)

    return {
        "time": now.isoformat(),
        "system": _try(lambda: tool("system_info")),
        "weather": _try(lambda: tool("weather_current")),
        "next_events": personal(lambda: (tool("calendar_upcoming_events", days_ahead=1) or [])[:3]),
        "guest_mode": _try(lambda: guest.status(siris)),
        "energy": _try(lambda: energy_status(siris)),
        "parcels": personal(lambda: siris.parcels.active()),
        "car": personal(lambda: tool("car_status")),
        "cameras": _try(lambda: siris._frigate_client.list_cameras() if siris._frigate_client else []),
        "autonomy": _try(lambda: timeline(siris, hours=24)["items"][:8]),
        "brain": personal(lambda: siris.second_brain.learned_today(now.date().isoformat())[:8] if siris.second_brain else None),
        "recent": _try(lambda: [
            {"tool": a["tool_name"], "time": a["created_at"], "error": bool(a["error"])} for a in siris.audit.list_recent(limit=6)
        ]),
    }


@router.get("/hud/summary")
async def hud_summary(speaker: bool = False, siris: SirisAI = Depends(get_siris)) -> dict[str, Any]:
    """speaker=1: this is a HUD that can play sound (it's been tapped), so it can speak
    notifications at home (app/notify/routing.py); its pending ones come back too."""
    summary = await asyncio.to_thread(_summary, siris)
    if speaker:
        from app.notify.routing import note_hud_seen

        note_hud_seen(siris)
        router_ = getattr(siris, "_notify_client", None)
        summary["announcements"] = router_.pending() if hasattr(router_, "pending") else []
    return summary


@router.post("/hud/announcements/{announcement_id}/heard")
def hud_announcement_heard(announcement_id: int, siris: SirisAI = Depends(get_siris)) -> dict[str, Any]:
    """The HUD finished saying it, so it isn't pushed to the phone as well."""
    router_ = getattr(siris, "_notify_client", None)
    return {"heard": bool(hasattr(router_, "heard") and router_.heard(announcement_id))}


@router.get("/autonomy/timeline")
def autonomy_timeline(hours: float = 24, include_reads: bool = False, siris: SirisAI = Depends(get_siris)) -> dict[str, Any]:
    return timeline(siris, hours=max(1, min(hours, 24 * 14)), include_reads=include_reads)
