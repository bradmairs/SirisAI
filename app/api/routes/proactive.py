"""Proactive checks: composes existing skills with a notification, so
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
entry draws still holds. `event_timing_check` and `goal_check_in`
(app/skills/builtin/, later hardening passes) compose in the same way --
their warnings go through the same NotificationManager pass as
server_health_check's.
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
    # event_timing_check's and goal_check_in's own output (hardening
    # passes) -- composed alongside server_health_check the same way,
    # their warnings feeding the same NotificationManager pass below.
    event_timing: dict[str, Any]
    goal_check_in: dict[str, Any]
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
    """Run server_health_check, event_timing_check, and goal_check_in,
    score and correlate whatever warnings any of them finds
    (NotificationManager), and send one notification covering every
    "notify"/"urgent" item -- if a notify channel is configured
    (SIRISAI_NTFY_TOPIC) -- summarizing them. Meant to be triggered on a
    schedule from outside this process -- see the README for a crontab
    example. `notifications` in the response shows every item's
    classification regardless of whether anything was actually sent, so a
    caller can see what was judged worth surfacing (and what wasn't)."""
    trigger = "proactive check: automated schedule (see POST /siris/proactive/check)"
    result = siris.run_tool("server_health_check", {}, trigger=trigger)
    event_timing = siris.run_tool("event_timing_check", {}, trigger=trigger)
    goal_check_in = siris.run_tool("goal_check_in", {}, trigger=trigger)

    warnings = result["warnings"] + event_timing["warnings"] + goal_check_in["warnings"]
    items = NotificationManager(siris.entities).summarize(warnings) if warnings else []
    to_send = [item for item in items if item.action in ("notify", "urgent")]

    notified = False
    if to_send and siris.tools.get("notify") is not None:
        title = "SirisAI: urgent" if any(item.action == "urgent" for item in to_send) else "SirisAI: server health check"
        summary = "; ".join(item.message for item in to_send)
        # Evidence (milestone 28): the classified items that justified
        # sending, not just the notify call's own title/message.
        evidence = [{"message": item.message, "severity": item.severity, "action": item.action} for item in to_send]
        siris.run_tool("notify", {"title": title, "message": summary}, trigger=trigger, evidence=evidence)
        notified = True

    return HealthCheckResponse(
        result=result,
        event_timing=event_timing,
        goal_check_in=goal_check_in,
        notified=notified,
        notifications=[_to_response(i) for i in items],
    )


class BriefingResponse(BaseModel):
    result: dict[str, Any]
    notified: bool


@router.post("/briefing", response_model=BriefingResponse)
def run_daily_briefing(siris: SirisAI = Depends(get_siris)) -> BriefingResponse:
    """Run daily_briefing (milestone 39) and send its summary as one
    notification -- if a notify channel is configured (SIRISAI_NTFY_TOPIC).
    Meant to be triggered on a schedule from outside this process, same as
    POST /siris/proactive/check -- see the README for a crontab example.
    Unlike /check, always sends when a channel is configured (a briefing
    is the point of calling this at all, not a conditional alert), and
    doesn't go through NotificationManager -- there's nothing to score or
    correlate, `daily_briefing` already composed one summary."""
    trigger = "proactive check: automated schedule (see POST /siris/proactive/briefing)"
    result = siris.run_tool("daily_briefing", {}, trigger=trigger)

    notified = False
    if siris.tools.get("notify") is not None:
        siris.run_tool(
            "notify", {"title": "SirisAI: daily briefing", "message": result["summary"]}, trigger=trigger, evidence=result
        )
        notified = True

    return BriefingResponse(result=result, notified=notified)
