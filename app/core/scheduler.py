"""Background jobs: deliver due reminders/timers, watch SirisAI's own
health, and run the proactive checks (app/notify/proactive.py) that used
to need an external cron job. One asyncio task started with the app
(app/main.py); each job is a plain function so tests call it directly.
"""

import asyncio
import json
import logging
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
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
        try:
            from app.upkeep import issues

            issues.propose(siris, f"'{name}' keeps failing", f"The circuit breaker paused `{name}` after repeated failures. Latest error: {reason}", fingerprint=f"tool:{name}")
        except Exception:
            logger.exception("Proposing an issue for %s failed", name)
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


def run_server_watch(siris: "SirisAI") -> dict | None:
    """Server health (app/servers/watch.py), every SIRISAI_SERVER_WATCH_MINUTES."""
    if siris.ssh_client is None:
        return None
    from app.servers import watch

    try:
        return watch.check_health(siris)
    except Exception:
        logger.exception("Server watch failed")
        return None


def run_power_follow_up(siris: "SirisAI") -> list | None:
    """Did each reboot or shutdown happen, and is the server back (app/servers/power.py)?"""
    if siris.ssh_client is None:
        return None
    from app.servers import power

    try:
        return power.check(siris)
    except Exception:
        logger.exception("Power follow-up failed")
        return None


def run_device_watch(siris: "SirisAI") -> dict | None:
    """New devices on the network (app/homelab/devices.py)."""
    from app.homelab import devices

    if not devices.enabled(siris):
        return None
    try:
        return devices.check(siris)
    except Exception:
        logger.exception("Network device watch failed")
        return None


def run_camera_digest(siris: "SirisAI") -> dict | None:
    """What the cameras saw (app/household/cameras.py), at the digest hours."""
    from app.household import cameras

    try:
        return cameras.digest(siris) if cameras.due(siris) else None
    except Exception:
        logger.exception("Camera digest failed")
        return None


def run_health_checks(siris: "SirisAI") -> None:
    """The wind-down nudge (once an evening) and the stalled-import alert."""
    from app.health import insights, overview

    for name, job in (("wind-down", insights.wind_down_check), ("stale import", overview.check_stale)):
        try:
            job(siris)
        except Exception:
            logger.exception("Health %s check failed", name)


def run_speed_test(siris: "SirisAI") -> dict | None:
    from app.homelab import internet

    try:
        return internet.speed_test(siris)
    except Exception:
        logger.exception("Scheduled speed test failed")
        return None


async def _internet_loop(siris: "SirisAI") -> None:
    """The outage log's probe (app/homelab/internet.py), on its own so a long
    job in the main loop can't leave gaps in it."""
    from app.homelab import internet

    while True:
        interval = siris.settings.internet_probe_seconds
        if interval > 0:
            try:
                await asyncio.to_thread(internet.probe, siris)
            except Exception:
                logger.exception("Internet probe failed")
        await asyncio.sleep(interval if interval > 0 else 60)


def report_update(siris: "SirisAI") -> None:
    """Say how the last self-update went (app/upkeep/updates.py), once."""
    from app.upkeep import updates

    try:
        updates.report(siris)
    except Exception:
        logger.exception("Reporting the update outcome failed")


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


async def _run_brain_jobs(siris: "SirisAI") -> None:
    try:
        await brain_jobs.run_due(siris)
    except Exception:
        logger.exception("Second brain jobs failed")


def should_warm(siris: "SirisAI", now: datetime | None = None) -> bool:
    """Whether to keep the chat model loaded now: inside
    SIRISAI_OLLAMA_WARM_HOURS (4.6), so the first question of the morning
    doesn't wait for Ollama to load it."""
    hours = (siris.settings.ollama_warm_hours or "").strip()
    from app.cortex import trial

    if trial.arm(siris, now) == "old":  # a speed trial day without the changes
        return False
    if not hours or siris.llm is None or getattr(siris.llm, "warm", None) is None:
        return False
    try:
        start, end = (int(h) for h in hours.split("-", 1))
    except ValueError:
        return False
    hour = (now or datetime.now(UTC)).astimezone(clock.get_zone(siris.settings.timezone)).hour
    return start <= hour < end if start <= end else (hour >= start or hour < end)


# Each job runs on its own loop (4.6): before, ~20 jobs ran one after another
# in one loop, so a server check stuck on an SSH timeout held up reminders,
# the morning brief and everything else behind it. Now a slow job only delays
# itself. JOB_STATUS says when each last ran, how long it took, and whether
# it's still going (GET /siris/scheduler/jobs).
JOB_STATUS: dict[str, dict[str, object]] = {}
_EXECUTOR = ThreadPoolExecutor(max_workers=24, thread_name_prefix="siris-job")
# A job still going after this long is flagged "slow" in JOB_STATUS. It
# isn't stopped (a thread can't be), and it won't be started again until
# it finishes, so a hung check never piles up copies of itself.
SLOW_AFTER_SECONDS = 600


@dataclass
class Job:
    name: str
    call: Callable[["SirisAI"], object]
    every: Callable[["SirisAI"], float]  # seconds; 0 or less = off
    at_boot: bool = True  # False: first run one interval after startup
    is_async: bool = False


def _settings_every(attr: str, unit: float) -> Callable[["SirisAI"], float]:
    return lambda siris: float(getattr(siris.settings, attr)) * unit


def _jobs(interval_seconds: float) -> list[Job]:
    # Lambdas look the functions up by name when they run, so tests can
    # replace any of them on this module.
    every_tick = lambda _siris: interval_seconds  # noqa: E731
    return [
        Job("reminders", lambda s: tick(s, datetime.now(UTC)), every_tick),
        Job("tool_alerts", lambda s: alert_on_broken_tools(s, datetime.now(UTC)), lambda _s: _HEALTH_EVERY_SECONDS),
        Job("spoken_brief", _spoken_brief, every_tick),
        Job("proactive", lambda s: run_proactive_check(s, datetime.now(UTC)), _settings_every("proactive_check_interval_minutes", 60)),
        Job("presence", lambda s: run_presence_and_geofence_checks(s), _settings_every("presence_check_interval_minutes", 60)),
        Job("intruder", lambda s: run_intruder_check(s), _settings_every("intruder_check_interval_minutes", 60)),
        Job("self_healing", lambda s: run_self_healing(s), _settings_every("remediation_check_interval_minutes", 60)),
        Job("security", lambda s: run_security_watch(s), _settings_every("security_check_interval_hours", 3600), at_boot=False),
        Job("image_scan", lambda s: run_image_scan(s), _settings_every("security_scan_interval_days", 86400), at_boot=False),
        Job("habits", lambda s: run_habit_check(s), _settings_every("habit_check_interval_days", 86400), at_boot=False),
        Job("parcels", lambda s: run_parcel_refresh(s), _settings_every("parcel_refresh_minutes", 60)),
        Job("tool_choice_eval", lambda s: run_tool_choice_eval(s), _settings_every("eval_interval_days", 86400), at_boot=False),
        Job("attention", lambda s: (run_attention_sync(s), report_update(s)), _settings_every("attention_sync_minutes", 60), at_boot=False),
        Job("servers", lambda s: run_server_watch(s), _settings_every("server_watch_minutes", 60)),
        Job("power_follow_up", lambda s: run_power_follow_up(s), lambda _s: 30),
        Job("devices", lambda s: run_device_watch(s), _settings_every("network_device_watch_minutes", 60)),
        Job("camera_digest", lambda s: run_camera_digest(s), every_tick),
        Job("health_checks", lambda s: run_health_checks(s), lambda _s: 300),
        Job("speed_test", lambda s: run_speed_test(s), _settings_every("internet_speedtest_hours", 3600), at_boot=False),
        Job("daily_jobs", lambda s: run_daily_jobs(s), every_tick),
        Job("brain_watch", lambda s: _brain_watch(s), lambda _s: 900, at_boot=False),
        Job("warm_model", _warm, lambda _s: 240, is_async=True),
        Job("speed_trial", lambda s: _finish_trial(s), lambda _s: 3600),
        Job("house_watch", lambda s: _house_watch(s), lambda s: s.settings.house_watch_seconds),
        Job("conversation_summaries", _summaries, lambda _s: 600, at_boot=False, is_async=True),
    ]


def _brain_watch(siris: "SirisAI") -> None:
    if siris.second_brain is None:
        return
    from app.second_brain import watch

    watch.check(siris)


def _spoken_brief(siris: "SirisAI") -> None:
    from app.voice import morning

    if morning.configured(siris):
        morning.check(siris)


async def _warm(siris: "SirisAI") -> None:
    if should_warm(siris):
        assert siris.llm is not None
        await siris.llm.warm()  # type: ignore[attr-defined]


def _house_watch(siris: "SirisAI") -> None:
    if getattr(siris, "_home_assistant_client", None) is None:
        return
    from app.household import watch

    watch.check(siris)


def _finish_trial(siris: "SirisAI") -> None:
    from app.cortex import trial

    trial.finish_if_due(siris)


async def _summaries(siris: "SirisAI") -> None:
    from app.conversation import summaries

    await summaries.run(siris)


async def _job_loop(siris: "SirisAI", job: Job, tick_seconds: float) -> None:
    loop = asyncio.get_running_loop()
    last = 0.0 if job.at_boot else time.monotonic()
    running: asyncio.Future | None = None
    status = JOB_STATUS.setdefault(job.name, {"runs": 0})
    while True:
        try:
            every = job.every(siris)
        except Exception:  # noqa: BLE001 - a bad setting turns the job off, not the loop
            every = 0
        if running is not None and not running.done():
            started = status.get("started_monotonic") or time.monotonic()
            status["slow"] = time.monotonic() - float(started) > SLOW_AFTER_SECONDS  # type: ignore[arg-type]
        elif every > 0 and time.monotonic() - last >= every:
            last = time.monotonic()
            began = time.monotonic()
            status.update(started_monotonic=began, last_start=datetime.now(UTC).isoformat(), running=True, slow=False)
            running = asyncio.ensure_future(job.call(siris)) if job.is_async else loop.run_in_executor(_EXECUTOR, job.call, siris)
            try:
                # Wait a short while so quick jobs finish inside this pass;
                # anything longer carries on while the loop keeps ticking.
                await asyncio.wait({running}, timeout=max(tick_seconds, 1.0))
            except asyncio.CancelledError:
                running.cancel()
                raise
        if running is not None and running.done() and status.get("running"):
            status["running"] = False
            status["runs"] = int(status.get("runs") or 0) + 1  # type: ignore[call-overload]
            status["last_seconds"] = round(time.monotonic() - float(status["started_monotonic"]), 2)  # type: ignore[arg-type]
            error = running.exception() if not running.cancelled() else None
            status["last_error"] = repr(error) if error else None
            if error is not None:
                logger.error("Scheduled job %s failed", job.name, exc_info=error)
                _note_failure(siris, job.name, repr(error))
            elif float(status["last_seconds"]) > SLOW_AFTER_SECONDS:  # type: ignore[arg-type]
                _note_failure(siris, job.name, f"took {status['last_seconds']} s", slow=True)
        await asyncio.sleep(tick_seconds)


def _note_failure(siris: "SirisAI", name: str, error: str, slow: bool = False) -> None:
    """For the weekly reliability report (app/upkeep/reliability.py)."""
    try:
        from app.upkeep import reliability

        reliability.note_job_failure(siris, name, error, slow)
    except Exception:  # noqa: BLE001
        logger.debug("Noting a job failure failed", exc_info=True)


def job_status() -> list[dict[str, object]]:
    return [
        {"name": name, **{k: v for k, v in status.items() if k != "started_monotonic"}}
        for name, status in sorted(JOB_STATUS.items())
    ]


async def run(siris: "SirisAI", interval_seconds: float = 20.0) -> None:
    tasks = [asyncio.create_task(_job_loop(siris, job, interval_seconds)) for job in _jobs(interval_seconds)]
    tasks.append(asyncio.create_task(_internet_loop(siris)))
    brain_task: asyncio.Task | None = None
    try:
        while True:
            # Second brain growth jobs: each decides for itself whether it's due today.
            # They run beside the other jobs -- the OneNote import alone can take the
            # better part of an hour. Never two at once, so their order (import before
            # upkeep) holds.
            if siris.second_brain is not None and (brain_task is None or brain_task.done()):
                brain_task = asyncio.create_task(_run_brain_jobs(siris))
            await asyncio.sleep(interval_seconds)
    finally:
        for task in tasks:
            task.cancel()
        if brain_task is not None:
            brain_task.cancel()
