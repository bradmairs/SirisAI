"""Cron jobs that didn't run, or failed (6.38).

Every SIRISAI_CRON_WATCH_MINUTES (30; 0 off) SirisAI reads each cron
server's overview (app/cron/client.py) and keeps it, so the Cron tab can
show it at once, then raises:

- **didn't run**: a job that runs at most hourly, whose last expected run
  (more than 15 minutes ago) isn't in the cron log. Only judged when the log
  goes back before that run, so a server whose log can't be read, or was
  just rotated, isn't blamed. "The 03:00 backup on nas didn't run".
- **failed**: a watched job (`cron_watch_job` wraps it so its exit code is
  logged with `logger -t siris-cron`) whose last run exited non-zero.

Both clear by themselves once the job runs (or succeeds) again.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from app.cron.client import overview
from app.cron.schedule import prev_run

if TYPE_CHECKING:
    from app.core.siris import SirisAI

logger = logging.getLogger(__name__)
CACHE_KEY = "cron:overview"
GRACE = timedelta(minutes=15)


def cached(siris: "SirisAI") -> dict[str, Any] | None:
    return siris.state.get(CACHE_KEY)


def refresh(siris: "SirisAI", now: datetime | None = None) -> list[dict[str, Any]]:
    if siris.cron_client is None:
        return []
    now = now or datetime.now(UTC)
    hosts = overview(siris.cron_client, now)
    siris.state.set(CACHE_KEY, {"at": now.isoformat(), "hosts": hosts})
    try:
        from app.servers import timeline

        timeline.cron_runs(siris, hosts)
    except Exception:  # noqa: BLE001
        logger.debug("Couldn't add cron runs to the server timeline", exc_info=True)
    return hosts


def _when(text: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(str(text)) if text else None
    except ValueError:
        return None


def _offset(host: dict[str, Any]) -> int:
    from app.cron.client import _offset_minutes

    return _offset_minutes(host.get("utc_offset") or "")


def _hourly_or_rarer(schedule: str, at: datetime, offset: int) -> bool:
    last = prev_run(schedule, at, offset)
    before = prev_run(schedule, last - timedelta(minutes=1), offset) if last else None
    return bool(last and before and last - before >= timedelta(minutes=55))


def _name(job: dict[str, Any]) -> str:
    text = job.get("inner") or job["command"]
    return text if len(text) <= 60 else text[:57] + "..."


def problems(hosts: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    wanted = []
    for host in hosts:
        if not host.get("reachable"):
            continue
        offset = _offset(host)
        since = _when(host.get("log_since"))
        for job in host.get("jobs") or []:
            if not job.get("enabled"):
                continue
            key_base = f"cron:{host['host']}:{job['line'][:120]}"
            expected = prev_run(job["schedule"], now - GRACE, offset)
            last = _when(job.get("last_run"))
            if (expected and since and since <= expected and _hourly_or_rarer(job["schedule"], now - GRACE, offset)
                    and (last is None or last < expected - timedelta(minutes=2))):
                wanted.append({
                    "key": f"{key_base}:missed", "kind": "alert", "severity": "attention",
                    "title": f"{_name(job)} on {host['host']} didn't run at {expected.strftime('%a %H:%M')}",
                    "body": f"It's set for {job['description'].lower()} ({job['schedule']}), as {job.get('run_as')}, "
                            f"from {job.get('source')}. The cron log has no run then"
                            + (f"; it last ran {last.strftime('%a %-d %b %H:%M')}." if last else "."),
                })
            if job.get("watched") and job.get("last_exit") not in (None, 0):
                at = _when(job.get("last_exit_at"))
                wanted.append({
                    "key": f"{key_base}:failed", "kind": "alert", "severity": "attention",
                    "title": f"{_name(job)} on {host['host']} failed (exit {job['last_exit']})",
                    "body": f"Its last run{' at ' + at.strftime('%a %H:%M') if at else ''} ended with exit code {job['last_exit']}. "
                            "It clears when the next run succeeds.",
                })
    return wanted


def check(siris: "SirisAI", now: datetime | None = None) -> int:
    """Refresh the overview and raise what's wrong; returns how many problems."""
    if siris.cron_client is None:
        return 0
    now = now or datetime.now(UTC)
    wanted = problems(refresh(siris, now), now)
    siris.attention.sync("cron", wanted, outcome="Ran as expected")
    return len(wanted)
