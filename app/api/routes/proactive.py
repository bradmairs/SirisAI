"""Proactive checks: composes existing skills with a notification, so
something can run unprompted rather than only in response to a chat turn.

Delivered two ways, not mutually exclusive: the background scheduler
(app/core/scheduler.py, SIRISAI_PROACTIVE_CHECK_INTERVAL_MINUTES /
SIRISAI_BRIEFING_HOUR_LOCAL) calls the same functions these routes wrap
(app/notify/proactive.py) on its own cadence, and these routes stay
callable directly too -- an external cron/systemd timer, or just testing
one check by hand. Neither is required for the other to work.

What runs on that cadence got smarter in milestone 26 (app/notify/manager.py,
`NotificationManager`): rather than joining every warning into one flat
message, each warning is scored and correlated -- still deterministic
scoring, not fuzzy LLM judgment, so the "stops well short of autonomous
action" boundary this milestone's roadmap entry draws still holds. Only
"urgent" items actually push immediately; "notify"-tier items are queued
instead (an interruption controller, milestone 72, app/notify/pending.py)
and surface in the next daily_briefing rather than interrupting the user
for something that can wait. `event_timing_check`, `goal_check_in`,
`goal_decomposition_check`, `new_device_alert`, and `event_reaction_check`
(app/skills/builtin/, later hardening passes) compose in the same way --
their warnings go through the same NotificationManager pass as
server_health_check's. `event_reaction_check` is what closes the
"proactively notify based on an incoming event" gap
app/api/routes/events.py's own module docstring originally left open, via
a deterministic keyword scan rather than per-event LLM judgment -- see
its own module docstring for why.

POST /briefing (milestone 53) also speaks its summary aloud via
speak_in_current_room, alongside the push notification -- a second,
independent delivery channel, not a replacement; either, both, or
neither fire depending on what's configured/currently reachable.

POST /geofence (a Jarvis-concept brainstorm item) is a separate route
rather than composed into /check, same reasoning as /presence: it only
exists when Home Assistant is configured, and fires a user-authored
reminder message directly rather than something NotificationManager
needs to classify.
"""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.core.auth import require_api_key
from app.core.siris import SirisAI, get_siris
from app.core.user_context import resolve_user_id
from app.notify import proactive
from app.notify.manager import NotificationItem

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
    # event_timing_check's, goal_check_in's, new_device_alert's, and
    # event_reaction_check's own output (hardening passes) -- composed
    # alongside server_health_check the same way, their warnings feeding
    # the same NotificationManager pass below.
    event_timing: dict[str, Any]
    goal_check_in: dict[str, Any]
    goal_decomposition: dict[str, Any]
    new_device_alert: dict[str, Any]
    event_reaction: dict[str, Any]
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
    """Run server_health_check, event_timing_check, goal_check_in,
    goal_decomposition_check, new_device_alert, and event_reaction_check,
    score and correlate whatever warnings any of them finds
    (NotificationManager), push "urgent" items immediately -- if a notify
    channel is configured (SIRISAI_NTFY_TOPIC) -- and queue "notify"-tier
    items instead of pushing them. Runs automatically every
    SIRISAI_PROACTIVE_CHECK_INTERVAL_MINUTES (app/core/scheduler.py); this
    route exists for calling it directly too (an external schedule, or
    just testing it). `notifications` in the response shows every item's
    classification regardless of whether anything was actually sent or
    queued, so a caller can see what was judged worth surfacing (and what
    wasn't)."""
    trigger = "proactive check: POST /siris/proactive/check"
    outcome = proactive.run_health_check(siris, trigger)
    return HealthCheckResponse(
        result=outcome.result,
        event_timing=outcome.event_timing,
        goal_check_in=outcome.goal_check_in,
        goal_decomposition=outcome.goal_decomposition,
        new_device_alert=outcome.new_device_alert,
        event_reaction=outcome.event_reaction,
        notified=outcome.notified,
        queued=outcome.queued,
        notifications=[_to_response(i) for i in outcome.notifications],
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
    both independently. Runs automatically once a day at
    SIRISAI_BRIEFING_HOUR_LOCAL (app/core/scheduler.py); this route exists
    for calling it directly too. Unlike /check, always sends/speaks when a
    channel is configured (a briefing is the point of calling this at
    all, not a conditional alert), and doesn't go through
    NotificationManager -- there's nothing to score or correlate,
    `daily_briefing` already composed one summary."""
    trigger = "proactive check: POST /siris/proactive/briefing"
    outcome = proactive.run_daily_briefing(siris, trigger)
    return BriefingResponse(result=outcome.result, notified=outcome.notified, spoken=outcome.spoken)


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
    presence data to check in the first place. Runs automatically every
    SIRISAI_PRESENCE_CHECK_INTERVAL_MINUTES (app/core/scheduler.py) when
    configured -- a shorter interval than the health check needs, since a
    presence transition is worth catching quickly."""
    trigger = "proactive check: POST /siris/proactive/presence"
    result = proactive.run_presence_check(siris, trigger)
    if result is None:
        raise HTTPException(
            status_code=404,
            detail="Presence tracking isn't configured (needs UniFi settings and SIRISAI_PRESENCE_DEVICE_NAMES).",
        )
    return PresenceCheckResponse(result=result)


class GeofenceCheckResponse(BaseModel):
    result: dict[str, Any]


@router.post("/geofence", response_model=GeofenceCheckResponse)
def run_geofence_reminder_check(siris: SirisAI = Depends(get_siris)) -> GeofenceCheckResponse:
    """Run geofence_reminder_check (a Jarvis-concept brainstorm item):
    check every enabled GeofenceReminder's entity for a transition into
    its watched zone, sending a notification directly for any that just
    fired -- not routed through NotificationManager, since a reminder's
    message is arbitrary text the user already wrote and approved by
    creating it, not an auto-generated warning needing severity
    classification. 404 if Home Assistant isn't configured, same "only
    exists when its underlying capability does" reasoning
    POST /siris/proactive/presence already uses. Runs automatically every
    SIRISAI_PRESENCE_CHECK_INTERVAL_MINUTES when configured, same cadence
    as /presence -- both are "did a zone transition happen" checks."""
    trigger = "proactive check: POST /siris/proactive/geofence"
    result = proactive.run_geofence_check(siris, trigger)
    if result is None:
        raise HTTPException(
            status_code=404,
            detail="Geofence reminders aren't available (Home Assistant isn't configured).",
        )
    return GeofenceCheckResponse(result=result)
