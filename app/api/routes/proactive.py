"""Proactive checks: composes existing skills with a notification, so
something can run unprompted (e.g. from cron) rather than only in response
to a chat turn.

Deliberately not a background scheduler inside this process -- SirisAI
doesn't decide on its own when to check. Something external (a cron job,
a systemd timer) decides the cadence. What runs on that cadence got
smarter in milestone 26 (app/notify/manager.py, `NotificationManager`):
rather than joining every warning into one flat message, each warning is
scored and correlated -- still deterministic scoring, not fuzzy LLM
judgment, so the "stops well short of autonomous action" boundary this
milestone's roadmap entry draws still holds. Only "urgent" items actually
push immediately; "notify"-tier items are queued instead (an
interruption controller, milestone 72, app/notify/pending.py) and
surface in the next daily_briefing rather than interrupting the user for
something that can wait. `event_timing_check`, `goal_check_in`, and
`new_device_alert` (app/skills/builtin/, later hardening passes) compose
in the same way -- their warnings go through the same NotificationManager
pass as server_health_check's.

POST /briefing (milestone 53) also speaks its summary aloud via
speak_in_current_room, alongside the push notification -- a second,
independent delivery channel, not a replacement; either, both, or
neither fire depending on what's configured/currently reachable.
"""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.core.auth import require_api_key
from app.core.siris import SirisAI, get_siris
from app.core.user_context import resolve_user_id
from app.notify.manager import NotificationItem, NotificationManager

router = APIRouter(
    prefix="/siris/proactive",
    tags=["proactive"],
    dependencies=[Depends(require_api_key), Depends(resolve_user_id)],
)


class NotificationItemResponse(BaseModel):
    message: str
    severity: str
    category: str
    confidence: float
    action: str
    correlated_with: list[str]


class HealthCheckResponse(BaseModel):
    result: dict[str, Any]
    # event_timing_check's, goal_check_in's, and new_device_alert's own
    # output (hardening passes) -- composed alongside server_health_check
    # the same way, their warnings feeding the same NotificationManager
    # pass below.
    event_timing: dict[str, Any]
    goal_check_in: dict[str, Any]
    new_device_alert: dict[str, Any]
    notified: bool
    # Interruption controller (milestone 72): "notify"-tier items no
    # longer push immediately alongside "urgent" ones -- they're queued
    # here instead, to surface in the next daily_briefing rather than
    # interrupting the user for something that can wait.
    queued: int
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
def run_health_check_and_notify(
    siris: SirisAI = Depends(get_siris),
) -> HealthCheckResponse:
    """Run server_health_check, event_timing_check, goal_check_in, and
    new_device_alert, score and correlate whatever warnings any of them
    finds (NotificationManager), push "urgent" items immediately -- if a
    notify channel is configured (SIRISAI_NTFY_TOPIC) -- and queue
    "notify"-tier items instead of pushing them. Meant to be triggered on
    a schedule from outside this process -- see the README for a crontab
    example. `notifications` in the response shows every item's
    classification regardless of whether anything was actually sent or
    queued, so a caller can see what was judged worth surfacing (and what
    wasn't).

    Escalation tiers (milestone 44) plus an interruption controller
    (milestone 72): "urgent" still raises the notification's ntfy
    priority so it can break through a phone's do-not-disturb/silent
    settings, and now "notify"-tier items don't push at all -- they're
    queued (app/notify/pending.py) to surface in the next daily_briefing
    instead of interrupting the user for something that can wait for the
    digest. `queued` in the response is how many landed there this call."""
    trigger = "proactive check: automated schedule (see POST /siris/proactive/check)"
    result = siris.run_tool("server_health_check", {}, trigger=trigger)
    event_timing = siris.run_tool("event_timing_check", {}, trigger=trigger)
    goal_check_in = siris.run_tool("goal_check_in", {}, trigger=trigger)
    new_device_alert = siris.run_tool("new_device_alert", {}, trigger=trigger)

    warnings = (
        result["warnings"]
        + event_timing["warnings"]
        + goal_check_in["warnings"]
        + new_device_alert["warnings"]
    )
    items = NotificationManager(siris.entities).summarize(warnings) if warnings else []
    urgent_items = [item for item in items if item.action == "urgent"]
    deferred_items = [item for item in items if item.action == "notify"]

    notified = False
    if urgent_items and siris.tools.get("notify") is not None:
        title = "SirisAI: urgent"
        summary = "; ".join(item.message for item in urgent_items)
        # Evidence (milestone 28): the classified items that justified
        # sending, not just the notify call's own title/message.
        evidence = [
            {"message": item.message, "severity": item.severity, "action": item.action}
            for item in urgent_items
        ]
        siris.run_tool(
            "notify",
            {"title": title, "message": summary, "priority": "urgent"},
            trigger=trigger,
            evidence=evidence,
        )
        notified = True

    for item in deferred_items:
        siris.pending_notifications.queue(
            item.message, severity=item.severity, category=item.category
        )

    return HealthCheckResponse(
        result=result,
        event_timing=event_timing,
        goal_check_in=goal_check_in,
        new_device_alert=new_device_alert,
        notified=notified,
        queued=len(deferred_items),
        notifications=[_to_response(i) for i in items],
    )


class BriefingResponse(BaseModel):
    result: dict[str, Any]
    notified: bool
    # Voice delivery (milestone 53): whether the summary was actually
    # spoken aloud, not just whether speaking was attempted --
    # speak_in_current_room returns sent: False (not an error) when no
    # satellite is currently known, e.g. nobody's home to hear it.
    spoken: bool


@router.post("/briefing", response_model=BriefingResponse)
def run_daily_briefing(siris: SirisAI = Depends(get_siris)) -> BriefingResponse:
    """Run daily_briefing (milestone 39) and deliver its summary through
    whatever channels are configured -- if a notify channel is configured
    (SIRISAI_NTFY_TOPIC), and/or a voice satellite is currently known
    (milestone 53, via speak_in_current_room), always sends/speaks to
    both independently. Meant to be triggered on a schedule from outside
    this process, same as POST /siris/proactive/check -- see the README
    for a crontab example. Unlike /check, always sends/speaks when a
    channel is configured (a briefing is the point of calling this at
    all, not a conditional alert), and doesn't go through
    NotificationManager -- there's nothing to score or correlate,
    `daily_briefing` already composed one summary."""
    trigger = "proactive check: automated schedule (see POST /siris/proactive/briefing)"
    result = siris.run_tool("daily_briefing", {}, trigger=trigger)

    notified = False
    if siris.tools.get("notify") is not None:
        siris.run_tool(
            "notify",
            {"title": "SirisAI: daily briefing", "message": result["summary"]},
            trigger=trigger,
            evidence=result,
        )
        notified = True

    spoken = False
    if siris.tools.get("speak_in_current_room") is not None:
        speak_result = siris.run_tool(
            "speak_in_current_room",
            {"message": result["summary"]},
            trigger=trigger,
            evidence=result,
        )
        spoken = bool(speak_result.get("sent"))

    return BriefingResponse(result=result, notified=notified, spoken=spoken)


class PresenceCheckResponse(BaseModel):
    result: dict[str, Any]


@router.post("/presence", response_model=PresenceCheckResponse)
def run_presence_check(siris: SirisAI = Depends(get_siris)) -> PresenceCheckResponse:
    """Run presence_check (milestone 42): detect whether household
    presence changed since the last check and, on a real transition,
    activate the configured home/away Home Assistant scene. 404 if
    presence tracking isn't configured (SIRISAI_UNIFI_* +
    SIRISAI_PRESENCE_DEVICE_NAMES) -- unlike /check and /briefing, which
    are always registered, this tool only exists when there's UniFi
    presence data to check in the first place. Meant to be triggered
    frequently from outside this process (e.g. every few minutes) --
    same "no in-process scheduler" boundary as every other proactive
    route, just a shorter interval than a health check needs."""
    if siris.tools.get("presence_check") is None:
        raise HTTPException(
            status_code=404,
            detail="Presence tracking isn't configured (needs UniFi settings and SIRISAI_PRESENCE_DEVICE_NAMES).",
        )
    trigger = "proactive check: automated schedule (see POST /siris/proactive/presence)"
    result = siris.run_tool("presence_check", {}, trigger=trigger)
    return PresenceCheckResponse(result=result)
