"""The actual work behind each POST /siris/proactive/* route -- pulled out
of app/api/routes/proactive.py so the background scheduler
(app/core/scheduler.py) can run the exact same checks on its own cadence,
not a second reimplementation of them. Each route stays a thin wrapper:
call the matching function here, shape the result into its Pydantic
response model.

This *is* the "in-process scheduler" app/api/routes/proactive.py's own
module docstring said this project deliberately didn't have -- true when
that was written, no longer true since app/core/scheduler.py started
delivering reminders and circuit-breaker alerts. Extending it to the
checks that already existed for exactly this purpose (composing skills
with a notification, to run unprompted) is a continuation of that choice,
not a new one; an external cron/systemd timer still works too, calling
the same HTTP routes -- nothing here requires the in-process scheduler
specifically.
"""

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from app.notify.manager import NotificationItem, NotificationManager

if TYPE_CHECKING:
    from app.core.siris import SirisAI


@dataclass
class HealthCheckResult:
    result: dict[str, Any]
    event_timing: dict[str, Any]
    goal_check_in: dict[str, Any]
    goal_decomposition: dict[str, Any]
    new_device_alert: dict[str, Any]
    event_reaction: dict[str, Any]
    notified: bool
    queued: int
    notifications: list[NotificationItem] = field(default_factory=list)


@dataclass
class BriefingResult:
    result: dict[str, Any]
    notified: bool
    spoken: bool


def run_health_check(siris: "SirisAI", trigger: str) -> HealthCheckResult:
    """server_health_check, event_timing_check, goal_check_in,
    goal_decomposition_check, new_device_alert, and event_reaction_check,
    scored and correlated (NotificationManager): "urgent" items push
    immediately (if a notify channel is configured), "notify"-tier items
    queue instead of pushing, to surface in the next daily briefing rather
    than interrupting for something that can wait."""
    result = siris.run_tool("server_health_check", {}, trigger=trigger)
    event_timing = siris.run_tool("event_timing_check", {}, trigger=trigger)
    goal_check_in = siris.run_tool("goal_check_in", {}, trigger=trigger)
    goal_decomposition = siris.run_tool("goal_decomposition_check", {}, trigger=trigger)
    new_device_alert = siris.run_tool("new_device_alert", {}, trigger=trigger)
    event_reaction = siris.run_tool("event_reaction_check", {}, trigger=trigger)

    warnings = (
        result["warnings"]
        + event_timing["warnings"]
        + goal_check_in["warnings"]
        + goal_decomposition["warnings"]
        + new_device_alert["warnings"]
        + event_reaction["warnings"]
    )
    items = NotificationManager(siris.entities).summarize(warnings) if warnings else []
    urgent_items = [item for item in items if item.action == "urgent"]
    deferred_items = [item for item in items if item.action == "notify"]

    notified = False
    if urgent_items and siris.tools.get("notify") is not None:
        evidence = [
            {"message": item.message, "severity": item.severity, "action": item.action}
            for item in urgent_items
        ]
        siris.run_tool(
            "notify",
            {"title": "SirisAI: urgent", "message": "; ".join(item.message for item in urgent_items), "priority": "urgent"},
            trigger=trigger,
            evidence=evidence,
        )
        notified = True

    for item in deferred_items:
        siris.pending_notifications.queue(item.message, severity=item.severity, category=item.category)

    return HealthCheckResult(
        result=result,
        event_timing=event_timing,
        goal_check_in=goal_check_in,
        goal_decomposition=goal_decomposition,
        new_device_alert=new_device_alert,
        event_reaction=event_reaction,
        notified=notified,
        queued=len(deferred_items),
        notifications=items,
    )


def run_daily_briefing(siris: "SirisAI", trigger: str) -> BriefingResult:
    """daily_briefing's summary, delivered through whatever's configured --
    push (if a notify channel is set) and/or spoken (if a satellite is
    currently known) -- independently of each other, always when
    reachable (unlike /check, this isn't conditional on anything being
    wrong; a briefing is the point of calling it)."""
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
        speak_result = siris.run_tool("speak_in_current_room", {"message": result["summary"]}, trigger=trigger, evidence=result)
        spoken = bool(speak_result.get("sent"))

    return BriefingResult(result=result, notified=notified, spoken=spoken)


def run_presence_check(siris: "SirisAI", trigger: str) -> dict[str, Any] | None:
    """None if presence tracking isn't configured (SIRISAI_UNIFI_* +
    SIRISAI_PRESENCE_DEVICE_NAMES) -- distinct from an empty/unchanged
    result, which is a real (if uneventful) check."""
    if siris.tools.get("presence_check") is None:
        return None
    return siris.run_tool("presence_check", {}, trigger=trigger)


def run_geofence_check(siris: "SirisAI", trigger: str) -> dict[str, Any] | None:
    """None if geofence reminders aren't available (Home Assistant isn't
    configured)."""
    if siris.tools.get("geofence_reminder_check") is None:
        return None
    return siris.run_tool("geofence_reminder_check", {}, trigger=trigger)
