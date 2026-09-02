"""Proactive checks: composes an existing skill with a notification, so
something can run unprompted (e.g. from cron) rather than only in response
to a chat turn.

Deliberately not a background scheduler inside this process -- SirisAI
doesn't decide on its own when to check or what to do about it. Something
external (a cron job, a systemd timer) decides the cadence, and the
decision to notify uses the same deterministic threshold logic
server_health_check (milestone 15) already has, not fuzzy LLM judgment.
That's the "stops well short of autonomous action" boundary this
milestone's roadmap entry draws.
"""

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.core.auth import require_api_key
from app.core.siris import SirisAI, get_siris

router = APIRouter(prefix="/siris/proactive", tags=["proactive"], dependencies=[Depends(require_api_key)])


class HealthCheckResponse(BaseModel):
    result: dict[str, Any]
    notified: bool


@router.post("/check", response_model=HealthCheckResponse)
def run_health_check_and_notify(siris: SirisAI = Depends(get_siris)) -> HealthCheckResponse:
    """Run server_health_check; if it found anything abnormal and a
    notify channel is configured (SIRISAI_NTFY_TOPIC), send one
    notification summarizing it. Meant to be triggered on a schedule from
    outside this process -- see the README for a crontab example."""
    result = siris.run_tool("server_health_check", {})

    notified = False
    if not result["healthy"] and siris.tools.get("notify") is not None:
        summary = "; ".join(result["warnings"])
        siris.run_tool("notify", {"title": "SirisAI: server health check", "message": summary})
        notified = True

    return HealthCheckResponse(result=result, notified=notified)
