"""Background jobs: deliver due reminders/timers, and watch SirisAI's own
health. One asyncio task started with the app (app/main.py); each job is a
plain function so tests call it directly.
"""

import asyncio
import logging
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from app.core.health import broken_tools
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


def tick(siris: "SirisAI", now: datetime | None = None, run_health: bool = False) -> dict[str, object]:
    result: dict[str, object] = {"reminders_sent": deliver_due_reminders(siris, now)}
    if run_health:
        result["tool_alerts"] = alert_on_broken_tools(siris, now)
    return result


async def run(siris: "SirisAI", interval_seconds: float = 20.0) -> None:
    last_health = 0.0
    while True:
        try:
            due_health = time.monotonic() - last_health >= _HEALTH_EVERY_SECONDS
            await asyncio.to_thread(tick, siris, datetime.now(UTC), due_health)
            if due_health:
                last_health = time.monotonic()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Scheduler tick failed")
        await asyncio.sleep(interval_seconds)
