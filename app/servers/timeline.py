"""Each server's timeline (6.38): what happened to it, newest first.

Reboots (started, back, didn't happen), updates installed, failed
services, full disks, memory, not answering, disk trouble, and its cron
jobs (the ones that run at most hourly, and missed or failed runs), so
"what happened to the Pi this week" has one answer: server_timeline, or
GET /siris/servers/{name}/timeline. Kept in state, 30 days or 300 events
per server.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.core.siris import SirisAI

STATE_KEY = "server:timeline"
SEEN_KEY = "server:timeline:seen"
KEEP = 300
KEEP_DAYS = 30


def add(siris: "SirisAI", server: str, kind: str, text: str, at: datetime | str | None = None) -> None:
    when = at if isinstance(at, str) else (at or datetime.now(UTC)).isoformat()
    data = dict(siris.state.get(STATE_KEY) or {})
    events = list(data.get(server) or [])
    if any(e["at"] == when and e["text"] == text for e in events[-20:]):
        return
    events.append({"at": when, "kind": kind, "text": text})
    cutoff = (datetime.now(UTC) - timedelta(days=KEEP_DAYS)).isoformat()
    data[server] = sorted((e for e in events if _utc(e["at"]) >= cutoff), key=lambda e: _utc(e["at"]))[-KEEP:]
    siris.state.set(STATE_KEY, data)


def _utc(at: str) -> str:
    try:
        when = datetime.fromisoformat(at)
    except ValueError:
        return at
    return (when if when.tzinfo else when.replace(tzinfo=UTC)).astimezone(UTC).isoformat()


def note_items(siris: "SirisAI", kind: str, wanted: list[dict[str, Any]], servers: list[str]) -> None:
    """Record each attention item the first time it's raised, against its server."""
    seen = dict(siris.state.get(SEEN_KEY) or {})
    now = datetime.now(UTC).isoformat()
    current = set()
    for item in wanted:
        server = (item.get("payload") or {}).get("server") or next(
            (s for s in servers if s in str(item.get("key", "")).split(":") or s in str(item.get("title", "")).split()), None)
        if not server:
            continue
        key = str(item["key"])
        current.add(key)
        if key not in seen:
            add(siris, server, kind, str(item["title"]))
            seen[key] = now
    for key in [k for k in seen if _kind_of(k) == kind and k not in current]:
        seen.pop(key, None)  # cleared: the next time it happens is new again
    siris.state.set(SEEN_KEY, seen)


def _kind_of(key: str) -> str:
    return "updates" if key.startswith("server-updates") else "disks" if key.startswith("server-disks") else "health"


def events(siris: "SirisAI", server: str, days: int = 7) -> list[dict[str, Any]]:
    cutoff = (datetime.now(UTC) - timedelta(days=max(1, days))).isoformat()
    return [e for e in reversed((siris.state.get(STATE_KEY) or {}).get(server) or []) if _utc(e["at"]) >= cutoff]


def servers(siris: "SirisAI") -> list[str]:
    return sorted((siris.state.get(STATE_KEY) or {}).keys())


def text(siris: "SirisAI", server: str, days: int = 7) -> str:
    from app.core import clock

    found = events(siris, server, days)
    if not found:
        return f"Nothing recorded for {server} in the last {days} days."
    zone = clock.get_zone(siris.settings.timezone)

    def when(at: str) -> str:
        try:
            return datetime.fromisoformat(at).astimezone(zone).strftime("%a %-d %b %H:%M")
        except ValueError:
            return at

    return f"{server}, the last {days} days (newest first): " + "; ".join(f"{when(e['at'])}: {e['text']}" for e in found[:40]) + "."


def cron_runs(siris: "SirisAI", hosts: list[dict[str, Any]]) -> None:
    """From the cron overview: the runs of jobs that run at most hourly, per server."""
    from datetime import timedelta as _td

    from app.cron.schedule import prev_run

    names = {}
    if siris.ssh_client is not None:
        names = {h.host: h.name for h in siris.ssh_client.hosts}
    for host in hosts:
        if not host.get("reachable"):
            continue
        server = names.get(host["host"].split("@")[-1], host["host"])
        for job in host.get("jobs") or []:
            if not job.get("last_run"):
                continue
            try:
                at = datetime.fromisoformat(job["last_run"])
            except ValueError:
                continue
            last = prev_run(job["schedule"], at)
            before = prev_run(job["schedule"], last - _td(minutes=1)) if last else None
            if last and before and last - before < _td(minutes=55):
                continue  # every few minutes: too often to list
            outcome = "" if job.get("last_exit") is None else (", ok" if job["last_exit"] == 0 else f", failed (exit {job['last_exit']})")
            name = job.get("inner") or job["command"]
            add(siris, server, "cron", f"cron ran {name[:60]}{outcome}", job["last_run"])
