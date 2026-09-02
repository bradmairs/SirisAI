"""Proactive checks: composes an existing skill with a notification, so
something can run unprompted (e.g. from cron) rather than only in response
to a chat turn.

Deliberately not a background scheduler inside this process -- SirisAI
doesn't decide on its own when to check. Something external (a cron job,
a systemd timer) decides the cadence. What runs on that cadence got
smarter in milestone 26 (app/notify/manager.py, `NotificationManager`):
rather than joining every warning into one flat message, each warning is
scored and correlated, and only "notify"/"urgent" items actually get
pushed -- still deterministic scoring, not fuzzy LLM judgment, so the
"stops well short of autonomous action" boundary this milestone's roadmap
entry draws still holds.
"""

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.core.auth import require_api_key
from app.core.siris import SirisAI, get_siris
from app.notify.manager import NotificationItem, NotificationManager

router = APIRouter(prefix="/siris/proactive", tags=["proactive"], dependencies=[Depends(require_api_key)])


class NotificationItemResponse(BaseModel):
    message: str
    severity: str
    category: str
    confidence: float
    action: str
    correlated_with: list[str]


class HealthCheckResponse(BaseModel):
    result: dict[str, Any]
    notified: bool
    notifications: list[NotificationItemResponse]


def _to_response(item: NotificationItem) -> NotificationItemResponse:
    return NotificationItemResponse(
        message=item.message,
        severity=item.severity,
        category=item.category,
        confidence=item.confidence,
        action=item.action,
        correlated_with=item.correlated_with,
    )


@router.post("/check", response_model=HealthCheckResponse)
def run_health_check_and_notify(siris: SirisAI = Depends(get_siris)) -> HealthCheckResponse:
    """Run server_health_check, score and correlate whatever warnings it
    finds (NotificationManager), and send one notification covering every
    "notify"/"urgent" item -- if a notify channel is configured
    (SIRISAI_NTFY_TOPIC) -- summarizing them. Meant to be triggered on a
    schedule from outside this process -- see the README for a crontab
    example. `notifications` in the response shows every item's
    classification regardless of whether anything was actually sent, so a
    caller can see what was judged worth surfacing (and what wasn't)."""
    result = siris.run_tool("server_health_check", {})

    items = NotificationManager(siris.entities).summarize(result["warnings"]) if result["warnings"] else []
    to_send = [item for item in items if item.action in ("notify", "urgent")]

    notified = False
    if to_send and siris.tools.get("notify") is not None:
        title = "SirisAI: urgent" if any(item.action == "urgent" for item in to_send) else "SirisAI: server health check"
        summary = "; ".join(item.message for item in to_send)
        siris.run_tool("notify", {"title": title, "message": summary})
        notified = True

    return HealthCheckResponse(result=result, notified=notified, notifications=[_to_response(i) for i in items])
