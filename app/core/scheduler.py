"""Background jobs: deliver due reminders/timers, watch SirisAI's own
health, and run the proactive checks (app/notify/proactive.py) that used
to need an external cron job. One asyncio task started with the app
(app/main.py); each job is a plain function so tests call it directly.
"""

import asyncio
import json
import logging
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from app.core import clock
from app.core.health import broken_tools
from app.notify import proactive
from app.remediation import runner as remediation
from app.anticipation import habits
from app.learning import feedback
from app.energy import service as energy
from app.reminders.service import MAX_ATTEMPTS
from app.second_brain import jobs as brain_jobs

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


def _has_geofence_reminders(siris: "SirisAI") -> bool:
    try:
        return bool(siris.geofence.list_all(enabled_only=True))
    except Exception:  # a stand-in SirisAI, or the store unavailable: let the check decide
        return True


def run_presence_and_geofence_checks(siris: "SirisAI") -> None:
    """presence_check and geofence_reminder_check, on
    SIRISAI_PRESENCE_CHECK_INTERVAL_MINUTES -- both "did a zone transition
    happen" checks, and both no-ops (None) when their underlying
    capability isn't configured."""
    for run_check, label in (
        (proactive.run_presence_check, "presence"),
        (proactive.run_geofence_check, "geofence"),
    ):
        if label == "geofence" and not _has_geofence_reminders(siris):
            continue  # nothing to watch: don't run (and log) a check every few minutes
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


def run_security_watch(siris: "SirisAI") -> dict | None:
    """security_briefing on a schedule, acting only on problems: an urgent
    push for failed/stale backups or a certificate about to expire, a note
    for the daily briefing for everything else."""
    try:
        report = siris.tools.get("security_briefing").run()  # type: ignore[union-attr]
    except Exception:
        logger.exception("Scheduled security check failed")
        return None
    trigger = "security watch: automated schedule"
    if report["urgent"]:
        if siris.tools.get("notify") is not None:
            siris.run_tool("notify", {"title": "SirisAI: security", "message": "; ".join(report["urgent"]), "priority": "urgent"}, trigger=trigger, evidence=report["urgent"])
        else:
            for line in report["urgent"]:
                siris.pending_notifications.queue(line, severity="urgent", category="security")
    for line in report["warnings"]:
        siris.pending_notifications.queue(line, severity="warning", category="security")
    return report


def run_image_scan(siris: "SirisAI") -> None:
    tool = siris.tools.get("security_scan_images")
    if tool is None:
        return
    try:
        tool.run()
    except Exception:
        logger.exception("Scheduled image scan failed")


def run_habit_check(siris: "SirisAI") -> list[dict]:
    """Mine habits and push each new suggestion, with one-tap Create/Dismiss
    buttons when SIRISAI_PUBLIC_BASE_URL is set."""
    if siris.tools.get("habit_suggestions") is None:
        return []
    try:
        fresh = habits.refresh(siris)
    except Exception:
        logger.exception("Scheduled habit check failed")
        return []
    for suggestion in fresh:
        actions = None
        if siris.settings.public_base_url:
            headers = {"Content-Type": "application/json"}
            if siris.settings.api_key:
                headers["Authorization"] = f"Bearer {siris.settings.api_key}"
            base = siris.settings.public_base_url.rstrip("/")
            body = json.dumps({"arguments": {"suggestion_id": suggestion["id"]}, "confirmed": True})
            actions = [
                {"action": "http", "label": "Create automation", "url": f"{base}/siris/tools/accept_habit_suggestion/run", "method": "POST", "headers": headers, "body": body},
                {"action": "http", "label": "No thanks", "url": f"{base}/siris/tools/dismiss_habit_suggestion/run", "method": "POST", "headers": headers, "body": body},
            ]
        message = f"{suggestion['description']} Want me to automate it?"
        if feedback.muted(siris, "attention:habits"):
            continue  # you've turned most of these down: it waits in the attention feed instead of buzzing your phone
        try:
            if siris.tools.get("notify") is not None:
                siris.run_tool("notify", {"title": "SirisAI: automation idea", "message": message, "actions": actions}, trigger="habit check: automated schedule", evidence=suggestion)
            else:
                siris.pending_notifications.queue(message, severity="info", category="habit")
        except Exception:
            logger.exception("Sending a habit suggestion failed")
    return fresh


def run_attention_sync(siris: "SirisAI") -> None:
    """Bring the attention feed's synced sources (habit suggestions, waiting
    jobs, queued notices, second-brain reviews) up to date."""
    from app.hub.attention import sync_all

    try:
        sync_all(siris)
    except Exception:
        logger.exception("Attention sync failed")


def run_parcel_refresh(siris: "SirisAI") -> list[dict]:
    """Refresh tracked parcels from 17TRACK and push the ones that just went
    out for delivery, arrived, or hit a problem."""
    if siris.parcel_tracker is None:
        return []
    from app.parcels import service as parcels

    try:
        changed = parcels.refresh(siris.parcels, siris.parcel_tracker)
    except Exception:
        logger.exception("Parcel refresh failed")
        return []
    for parcel in changed:
        message = f"{parcel['label']} is {parcel['status_text']}." + (f" {parcel['last_event']}" if parcel.get("last_event") else "")
        try:
            _push(siris, message, "SirisAI: parcel", priority="high" if parcel["status"] in ("OutForDelivery", "DeliveryFailure") else "default", category="parcels")
        except Exception:
            logger.exception("Parcel notification failed")
    return changed


def run_tool_choice_eval(siris: "SirisAI") -> dict | None:
    if siris.settings.llm_provider == "none":
        return None
    from app.evals import tool_choice

    try:
        return tool_choice.run_and_record(siris)
    except Exception:
        logger.exception("Scheduled tool-choice eval failed")
        return None


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


def run_deferred_jobs(siris: "SirisAI", now: datetime | None = None) -> list[dict]:
    try:
        return energy.run_due(siris, now)
    except Exception:
        logger.exception("Running deferred jobs failed")
        return []


def run_daily_jobs(siris: "SirisAI", now: datetime | None = None) -> dict:
    from app.core import daily_jobs

    try:
        return daily_jobs.run_due(siris, now)
    except Exception:
        logger.exception("Daily jobs failed")
        return {}


def tick(siris: "SirisAI", now: datetime | None = None, run_health: bool = False) -> dict[str, object]:
    result: dict[str, object] = {"reminders_sent": deliver_due_reminders(siris, now)}
    result["deferred_jobs"] = run_deferred_jobs(siris, now)
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
    last_security = time.monotonic()  # first check one interval after startup, not at boot
    last_scan = time.monotonic()
    last_habits = time.monotonic()
    last_parcels = 0.0
    last_eval = time.monotonic()
    last_attention = time.monotonic()  # the feed syncs itself when asked; no need to at boot
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

            security_interval = siris.settings.security_check_interval_hours * 3600
            if security_interval > 0 and time.monotonic() - last_security >= security_interval:
                await asyncio.to_thread(run_security_watch, siris)
                last_security = time.monotonic()

            scan_interval = siris.settings.security_scan_interval_days * 86400
            if scan_interval > 0 and time.monotonic() - last_scan >= scan_interval:
                await asyncio.to_thread(run_image_scan, siris)
                last_scan = time.monotonic()

            habit_interval = siris.settings.habit_check_interval_days * 86400
            if habit_interval > 0 and time.monotonic() - last_habits >= habit_interval:
                await asyncio.to_thread(run_habit_check, siris)
                last_habits = time.monotonic()

            parcel_interval = siris.settings.parcel_refresh_minutes * 60
            if parcel_interval > 0 and time.monotonic() - last_parcels >= parcel_interval:
                await asyncio.to_thread(run_parcel_refresh, siris)
                last_parcels = time.monotonic()

            eval_interval = siris.settings.eval_interval_days * 86400
            if eval_interval > 0 and time.monotonic() - last_eval >= eval_interval:
                await asyncio.to_thread(run_tool_choice_eval, siris)
                last_eval = time.monotonic()

            attention_interval = siris.settings.attention_sync_minutes * 60
            if attention_interval > 0 and time.monotonic() - last_attention >= attention_interval:
                await asyncio.to_thread(run_attention_sync, siris)
                last_attention = time.monotonic()

            # Nightly reflection, weekly goal review and the other once-a-day jobs.
            await asyncio.to_thread(run_daily_jobs, siris)

            # Second brain growth jobs: each decides for itself whether it's due today.
            if siris.second_brain is not None:
                await brain_jobs.run_due(siris)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Scheduler tick failed")
        await asyncio.sleep(interval_seconds)
