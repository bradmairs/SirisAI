"""Answers ready before they're asked (6.48).

SirisAI learns when each of a few everyday questions gets asked (the
weather, the calendar, how you slept and your readiness): from the last 14
days of the audit log, the local hours in which a tool was used on at least
3 days. Five minutes before such an hour, the `prefetch` job runs that tool
and keeps the answer; asked within SIRISAI_PREFETCH_FRESH_SECONDS (600) of
that, run_tool hands back the kept answer at once instead of calling the
service again. Kept in memory only, per user, and only for these read-only
tools called with the same arguments.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from app.core.user_context import get_current_user_id

if TYPE_CHECKING:
    from app.core.siris import SirisAI

logger = logging.getLogger(__name__)
TOOLS: dict[str, dict[str, Any]] = {
    "weather_current": {},
    "weather_forecast": {"days": 1},
    "calendar_upcoming_events": {"days_ahead": 1},
    "home_assistant_get_calendar_events": {"days": 1},
    "health_overview": {},
    "health_summary": {},
}
MIN_DAYS = 3
_lock = threading.Lock()
_kept: dict[tuple[str, str, str], tuple[float, Any]] = {}
_last_run: dict[str, str] = {}


def _key(name: str, arguments: dict[str, Any]) -> tuple[str, str, str]:
    return (get_current_user_id(), name, repr(sorted((arguments or {}).items())))


def get(siris: "SirisAI", name: str, arguments: dict[str, Any], now: float | None = None) -> tuple[bool, Any]:
    if name not in TOOLS or siris.settings.prefetch_fresh_seconds <= 0:
        return False, None
    now = time.monotonic() if now is None else now
    with _lock:
        hit = _kept.get(_key(name, arguments))
    if hit and now - hit[0] <= siris.settings.prefetch_fresh_seconds:
        return True, hit[1]
    return False, None


def keep(name: str, arguments: dict[str, Any], result: Any, now: float | None = None) -> None:
    with _lock:
        _kept[_key(name, arguments)] = (time.monotonic() if now is None else now, result)


def clear() -> None:
    with _lock:
        _kept.clear()
        _last_run.clear()


def usual_hours(siris: "SirisAI", now: datetime | None = None) -> dict[str, set[int]]:
    """Per tool, the local hours it was used in on at least MIN_DAYS of the last 14 days."""
    from app.core import clock

    now = now or datetime.now(UTC)
    zone = clock.get_zone(siris.settings.timezone)
    days: dict[tuple[str, int], set[str]] = {}
    for entry in siris.audit.since(now - timedelta(days=14), limit=5000):
        if entry["tool_name"] not in TOOLS or entry.get("error"):
            continue
        at = datetime.fromisoformat(entry["created_at"]).replace(tzinfo=UTC).astimezone(zone)
        days.setdefault((entry["tool_name"], at.hour), set()).add(at.date().isoformat())
    out: dict[str, set[int]] = {}
    for (name, hour), seen in days.items():
        if len(seen) >= MIN_DAYS:
            out.setdefault(name, set()).add(hour)
    return out


def run(siris: "SirisAI", now: datetime | None = None) -> list[str]:
    """In the 5 minutes before a usual hour, fetch and keep that tool's answer."""
    from app.core import clock

    if siris.settings.prefetch_fresh_seconds <= 0:
        return []
    now = now or datetime.now(UTC)
    local = now.astimezone(clock.get_zone(siris.settings.timezone))
    if local.minute < 55:
        return []
    coming = (local.hour + 1) % 24
    done = []
    for name, hours in usual_hours(siris, now).items():
        tool = siris.tools.get(name)
        stamp = f"{local.date().isoformat()}T{coming:02d}"
        if tool is None or coming not in hours or _last_run.get(name) == stamp:
            continue
        _last_run[name] = stamp
        try:
            keep(name, TOOLS[name], tool.run(**TOOLS[name]))
            done.append(name)
        except Exception as exc:  # noqa: BLE001 - it'll just be fetched when asked
            logger.info("Prefetching %s failed: %s", name, exc)
    return done
