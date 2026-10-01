"""Background jobs: deliver due reminders/timers, watch SirisAI's own
health, and run the proactive checks (app/notify/proactive.py) that used
to need an external cron job. One asyncio task started with the app
(app/main.py); each job is a plain function so tests call it directly.
"""

import asyncio
import logging
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from app.core import clock
from app.core.health import broken_tools
from app.notify import proactive
from app.remediation import runner as remediation
from app.reminders.service import MAX_ATTEMPTS

if TYPE_CHECKING:
    from app.core.siris import SirisAI

logger = logging.getLogger(__name__)

_ALERT_COOLDOWN_SECONDS = 24 * 3600
_HEALTH_EVERY_SECONDS = 300


def _push(siris: "SirisAI", message: str, title: str, priority: str = "default", category: str = "notify") -> None:
    if siris._notify_client is not None:
        siris._notify_client.send(message, title=title, priority=priority)
    else:
        siris.pending_notifications.queue(message, "notify", category)


def deliver_due_reminders(siris: "SirisAI", now: datetime | None = None) -> int:
    """Push every reminder that has come due. Returns how many were sent."""
    sent = 0
    for reminder in siris.reminders.due(now):
        try:
            _push(siris, reminder["message"], "Reminder", priority="high", category="reminder")
        except Exception:
            logger.exception("Delivering reminder %s failed", reminder["id"])
            status = siris.reminders.record_failure(reminder["id"])
            if status == "failed":
                logger.error("Reminder %s gave up after %s attempts", reminder["id"], MAX_ATTEMPTS)
            continue
        siris.reminders.mark_sent(reminder["id"])
        sent += 1
    return sent


def alert_on_broken_tools(siris: "SirisAI", now: datetime | None = None) -> list[str]:
    """Tell the user once a day about each tool the circuit breaker has
    paused (found live: Docker failed 15 times before anyone noticed)."""
    now_ts = time.time()
    alerted: list[str] = []
    for name in sorted(broken_tools(siris, now)):
        last = siris._tool_alerts.get(name, 0.0)
        if now_ts - last < _ALERT_COOLDOWN_SECONDS:
            continue
        recent = siris.audit.find_recent(name, limit=1)
        reason = (recent[0]["error"] or "unknown error")[:140] if recent else "unknown error"
        try:
            _push(siris, f"'{name}' keeps failing ({reason}). I've stopped offering it in chat for now.", "SirisAI tool problem", category="tool_health")
        except Exception:
            logger.exception("Sending tool-health alert for %s failed", name)
            continue
        siris._tool_alerts[name] = now_ts
        alerted.append(name)
    return alerted


def run_proactive_check(siris: "SirisAI", now: datetime | None = None) -> None:
    """The same check POST /siris/proactive/check runs, on
    SIRISAI_PROACTIVE_CHECK_INTERVAL_MINUTES instead of needing an
    external cron job. Best-effort: a failure here shouldn't take the
    scheduler down, same posture as everything else in this module."""
    try:
        proactive.run_health_check(siris, "proactive check: automated schedule")
    except Exception:
        logger.exception("Scheduled proactive check failed")


def run_presence_and_geofence_checks(siris: "SirisAI") -> None:
    """presence_check and geofence_reminder_check, on
    SIRISAI_PRESENCE_CHECK_INTERVAL_MINUTES -- both "did a zone transition
    happen" checks, and both no-ops (None) when their underlying
    capability isn't configured."""
    for run_check, label in (
        (proactive.run_presence_check, "presence"),
        (proactive.run_geofence_check, "geofence"),
    ):
        try:
            run_check(siris, f"proactive check: automated schedule ({label})")
        except Exception:
            logger.exception("Scheduled %s check failed", label)


def run_intruder_check(siris: "SirisAI") -> None:
    """intruder_check (app/skills/builtin/intruder.py), when Frigate is configured."""
    if siris.tools.get("intruder_check") is None:
        return
    try:
        siris.tools.get("intruder_check").run()  # type: ignore[union-attr]
    except Exception:
        logger.exception("Scheduled intruder check failed")


def run_self_healing(siris: "SirisAI") -> None:
    """Self-healing (app/remediation/runner.py), when containers are watched."""
    if siris._docker_client is None or not siris.settings.remediation_container_list():
        return
    try:
        remediation.run(siris)
    except Exception:
        logger.exception("Scheduled self-healing check failed")


def maybe_run_daily_briefing(siris: "SirisAI", now: datetime | None = None) -> bool:
    """Runs daily_briefing once, the first scheduler tick at or past
    SIRISAI_BRIEFING_HOUR_LOCAL on a calendar day it hasn't already run on
    (SchedulerStateStore, persisted -- see that module's own docstring for
    why this one needs a real row instead of an in-memory timer). Returns
    whether it ran. Disabled (does nothing, returns False) when
    SIRISAI_BRIEFING_HOUR_LOCAL is unset."""
    hour = siris.settings.briefing_hour_local
    if hour is None:
        return False
    local_now = (now or datetime.now(UTC)).astimezone(clock.get_zone(siris.settings.timezone))
    if local_now.hour < hour:
        return False
    today = local_now.date().isoformat()
    if siris.scheduler_state.last_briefing_date() == today:
        return False
    try:
        proactive.run_daily_briefing(siris, "proactive check: automated schedule (daily briefing)")
    except Exception:
        logger.exception("Scheduled daily briefing failed")
    # Marked sent even on failure -- a broken briefing should surface once
    # (in the logs) and get investigated, not retry every tick for the
    # rest of the day.
    siris.scheduler_state.mark_briefing_sent(today)
    return True


def tick(siris: "SirisAI", now: datetime | None = None, run_health: bool = False) -> dict[str, object]:
    result: dict[str, object] = {"reminders_sent": deliver_due_reminders(siris, now)}
    if run_health:
        result["tool_alerts"] = alert_on_broken_tools(siris, now)
    result["briefing_sent"] = maybe_run_daily_briefing(siris, now)
    return result


async def run(siris: "SirisAI", interval_seconds: float = 20.0) -> None:
    last_health = 0.0
    last_proactive = 0.0
    last_presence = 0.0
    last_intruder = 0.0
    last_healing = 0.0
    while True:
        try:
            due_health = time.monotonic() - last_health >= _HEALTH_EVERY_SECONDS
            await asyncio.to_thread(tick, siris, datetime.now(UTC), due_health)
            if due_health:
                last_health = time.monotonic()

            proactive_interval = siris.settings.proactive_check_interval_minutes * 60
            if proactive_interval > 0 and time.monotonic() - last_proactive >= proactive_interval:
                await asyncio.to_thread(run_proactive_check, siris, datetime.now(UTC))
                last_proactive = time.monotonic()

            presence_interval = siris.settings.presence_check_interval_minutes * 60
            if presence_interval > 0 and time.monotonic() - last_presence >= presence_interval:
                await asyncio.to_thread(run_presence_and_geofence_checks, siris)
                last_presence = time.monotonic()

            intruder_interval = siris.settings.intruder_check_interval_minutes * 60
            if intruder_interval > 0 and time.monotonic() - last_intruder >= intruder_interval:
                await asyncio.to_thread(run_intruder_check, siris)
                last_intruder = time.monotonic()

            healing_interval = siris.settings.remediation_check_interval_minutes * 60
            if healing_interval > 0 and time.monotonic() - last_healing >= healing_interval:
                await asyncio.to_thread(run_self_healing, siris)
                last_healing = time.monotonic()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Scheduler tick failed")
        await asyncio.sleep(interval_seconds)
