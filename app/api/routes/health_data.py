"""POST /siris/health/metrics -- health data from a phone. Built for an
Apple Shortcuts "Get Contents of URL" step after "Find Health Samples":

    POST /siris/health/metrics
    Authorization: Bearer <key>        X-Siris-User: brad
    {"date": "2026-10-01", "metrics": {"sleep_hours": 7.2, "steps": 8123, "resting_heart_rate": 57}}

A flat {"steps": 8123, ...} body works too; `date` defaults to today in
SIRISAI_TIMEZONE. Unknown metric names are reported back, not stored.
"""

from datetime import date as date_type
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from app.core import clock
from app.core.auth import require_api_key
from app.core.siris import SirisAI, get_siris
from app.core.user_context import get_current_user_id, resolve_user_id
from app.health.service import normalize, summary

router = APIRouter(prefix="/siris/health", tags=["health"], dependencies=[Depends(require_api_key), Depends(resolve_user_id)])


@router.post("/metrics")
async def post_metrics(request: Request, siris: SirisAI = Depends(get_siris)) -> dict[str, Any]:
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(status_code=422, detail="Send JSON.") from None
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="Send a JSON object.")
    raw_date = body.get("date")
    raw_metrics = body.get("metrics") if isinstance(body.get("metrics"), dict) else {k: v for k, v in body.items() if k != "date"}
    day = str(raw_date)[:10] if raw_date else clock.now_local(siris.settings.timezone).date().isoformat()
    try:
        date_type.fromisoformat(day)
    except ValueError:
        raise HTTPException(status_code=422, detail="date must be YYYY-MM-DD.") from None
    metrics, ignored = normalize(raw_metrics)
    if not metrics:
        raise HTTPException(status_code=422, detail=f"No recognised metrics (got: {', '.join(ignored) or 'nothing'}).")
    siris.health.record(metrics, day)
    return {"user_id": get_current_user_id(), "date": day, "stored": metrics, "ignored": ignored}


@router.get("/summary")
def get_summary(siris: SirisAI = Depends(get_siris)) -> dict[str, Any]:
    return summary(siris.health, clock.now_local(siris.settings.timezone).date())
