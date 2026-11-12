"""Brain watch (5.83): notice when the second brain's jobs go quiet, and say so once.

The Obsidian sync stopped for four days in November 2026 (its crontab line was wiped) and
nothing noticed. Every 15 minutes this looks at:

- **Obsidian sync, server inventory, secrets backup:** the heartbeats the vault's own scripts
  write to `.siris-heartbeats.json` (tools/upkeep.py). A job that has never run isn't an alert.
- **Dream and upkeep:** the last day each ran, from the job state (app/second_brain/jobs.py).
- **GitHub sync:** commits waiting to be pushed for longer than brain_watch_push_hours, or
  a sync error that persists.

A problem is pushed to the phone when it starts, again each day it continues, and a short
"back to normal" when it clears. Nothing is retried or fixed here; it only tells.
"""
from __future__ import annotations

import json
import logging
import subprocess
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any

from app.core import clock

if TYPE_CHECKING:
    from app.core.siris import SirisAI

logger = logging.getLogger(__name__)

ALERTED_KEY = "second_brain_watch_alerted"   # {check: ISO time it was last alerted}
_REPEAT = timedelta(hours=24)
_STARTED = datetime.now(UTC)   # roughly when SirisAI started: a fresh start hasn't pushed yet


def _parse(iso: str | None, tz=UTC) -> datetime | None:
    """An ISO time; one without a zone is in `tz` (the vault's scripts write local time)."""
    if not iso:
        return None
    try:
        when = datetime.fromisoformat(iso)
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=tz)


def _ago(delta: timedelta) -> str:
    hours = delta.total_seconds() / 3600
    return f"{hours:.0f} hours" if hours < 48 else f"{hours / 24:.0f} days"


def heartbeats(siris: "SirisAI") -> dict[str, Any]:
    brain = siris.second_brain
    path = brain.path / ".siris-heartbeats.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _unpushed(siris: "SirisAI") -> int:
    brain = siris.second_brain
    try:
        out = subprocess.run(["git", "-C", str(brain.path), "rev-list", "--count", "@{u}..HEAD"],
                             capture_output=True, text=True, timeout=20)
        return int(out.stdout.strip() or 0) if out.returncode == 0 else 0
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return 0


def problems(siris: "SirisAI", now: datetime | None = None) -> dict[str, str]:
    """{check: what's wrong} for everything that needs attention right now."""
    s = siris.settings
    now = now or datetime.now(UTC)
    tz = clock.get_zone(s.timezone)
    found: dict[str, str] = {}
    beats = heartbeats(siris)
    for name, label, limit in (
        ("obsidian-sync", "The Obsidian sync", timedelta(hours=s.brain_watch_sync_hours)),
        ("server-inventory", "The nightly server inventory", timedelta(hours=s.brain_watch_inventory_hours)),
        ("secrets-backup", "The encrypted secrets backup", timedelta(days=s.brain_watch_backup_days)),
    ):
        b = beats.get(name)
        if not b:
            continue
        last_ok = _parse(b.get("last_ok"), tz)
        if last_ok is None or now - last_ok > limit:
            since = f" for {_ago(now - last_ok)}" if last_ok else ""
            why = f" Last error: {b['error']}" if b.get("error") else ""
            found[name] = f"{label} hasn't succeeded{since}.{why}"
    from app.second_brain.jobs import _last_runs

    runs = _last_runs(siris)
    today = now.astimezone(tz).date()
    for job, hour, label in (("dream", s.brain_dream_hour_local, "The nightly dream"),
                             ("upkeep", s.brain_upkeep_hour_local, "The nightly upkeep")):
        if hour is None or not runs.get(job):
            continue
        last = date.fromisoformat(runs[job])
        if (today - last).days >= 2:
            found[job] = f"{label} last ran on {last:%d %b} ({(today - last).days} days ago)."
    brain = siris.second_brain
    if brain.git_sync:
        ahead = _unpushed(siris)
        last_push = datetime.fromtimestamp(brain.last_push_ok, UTC) if brain.last_push_ok else None
        stuck = ahead and (last_push is None or now - last_push > timedelta(hours=s.brain_watch_push_hours))
        if stuck and (brain.last_sync_error or last_push is not None or _boot_age(siris, now) > timedelta(
                hours=s.brain_watch_push_hours)):
            err = f" {brain.last_sync_error}" if brain.last_sync_error else ""
            found["github"] = f"{ahead} brain commit{'s' if ahead != 1 else ''} haven't reached GitHub.{err}"
    return found


def _boot_age(siris: "SirisAI", now: datetime) -> timedelta:
    return now - _STARTED


def check(siris: "SirisAI", now: datetime | None = None) -> dict[str, Any]:
    """Alert on new problems (and daily while they last), and say when they clear."""
    if siris.second_brain is None:
        return {}
    from app.second_brain.jobs import _push

    now = now or datetime.now(UTC)
    current = problems(siris, now)
    alerted: dict[str, str] = dict(siris.state.get(ALERTED_KEY) or {})
    sent: list[str] = []
    due = [k for k in current if not alerted.get(k) or now - (_parse(alerted[k]) or now) >= _REPEAT]
    if due:
        _push(siris, "\n".join(current[k] for k in due), "Siris: brain needs a look", priority="high")
        for k in due:
            alerted[k] = now.isoformat()
        sent += due
    cleared = [k for k in alerted if k not in current]
    if cleared:
        _push(siris, "Back to normal: " + ", ".join(k.replace("-", " ") for k in cleared) + ".", "Siris: brain OK")
        for k in cleared:
            alerted.pop(k)
    siris.state.set(ALERTED_KEY, alerted)
    return {"problems": current, "alerted": sent, "cleared": cleared}
