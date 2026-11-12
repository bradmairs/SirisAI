"""Breakage watch (5.8): tell Brad within the hour when something stops working, and how to fix it.

Until now these surfaced in the Sunday reliability report: the house watch got 401s from Home
Assistant for days, and UniFi refusals went unnoticed. Every 15 minutes this looks at the last hour:

- **Background jobs** whose latest run failed, and failed at least twice in the last three hours
  (one blip isn't a breakage).
- **Tools** with at least three failures that point at something broken -- an expired key (401/403),
  a refused connection, a timeout, a server error -- spread over 20 minutes or more, none of them
  followed by a success. The gateway's nightly restart (a few minutes of refusals) doesn't count.
  Tools the circuit breaker has already paused are left to its own alert (app/core/scheduler.py).

A breakage is pushed when it starts, again each day it continues, and once more when it's proved
fixed (a successful call, or the job's next run passing); one that just stops showing up is cleared
quietly after a day.
Nothing is retried or changed here: it only tells, with the likely fix.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from app.db.models import ToolExecution

if TYPE_CHECKING:
    from app.core.siris import SirisAI

logger = logging.getLogger(__name__)

STATE_KEY = "breakage_watch"     # {key: {"title", "since", "pushed"}}
FEED_PREFIX = "breakage:"
WINDOW = timedelta(hours=1)
MIN_SPREAD = timedelta(minutes=20)
REPEAT_EVERY = timedelta(days=1)
MIN_TOOL_FAILURES = 3
MIN_JOB_FAILURES = 2
IGNORED_TOOLS = {"notify"}   # a failed push already lands in the feed (app/plugins/builtin/notify.py)

_AUTH = re.compile(r"\b40[13]\b|unauthori[sz]ed|forbidden|invalid (api )?(key|token)|expired", re.I)
_DOWN = re.compile(r"refused|unreachable|name or service|no route|errno 11[13]|connecterror", re.I)
_SLOW = re.compile(r"timed? ?out|timeout", re.I)
_SERVER = re.compile(r"\b5\d\d\b|server error|bad gateway|service unavailable", re.I)


def _kind(error: str) -> str | None:
    for kind, pattern in (("auth", _AUTH), ("down", _DOWN), ("slow", _SLOW), ("server", _SERVER)):
        if pattern.search(error or ""):
            return kind
    return None


def _service(name: str) -> str:
    for prefix, label in (("home_assistant", "Home Assistant"), ("house_watch", "Home Assistant"), ("unifi", "UniFi"),
                          ("presence", "UniFi"), ("docker", "Docker"), ("plex", "Plex"), ("sonarr", "Sonarr"),
                          ("radarr", "Radarr"), ("frigate", "Frigate"), ("synology", "the Synology NAS"),
                          ("ssh", "the servers over SSH"), ("calendar", "the calendar"), ("email", "email"),
                          ("weather", "the weather service"), ("web_", "web search")):
        if name.startswith(prefix):
            return label
    return name.replace("_", " ")


def advice(name: str, error: str) -> str:
    service = _service(name)
    kind = _kind(error)
    if kind == "auth":
        return (f"{service} is refusing SirisAI's key or token: it has probably expired or been replaced. "
                f"Make a new one in {service} and update it in SirisAI's settings.")
    if kind == "down":
        return f"{service} isn't answering: check it's running and reachable from the server."
    if kind == "slow":
        return f"{service} is too slow to answer: check it isn't overloaded or half-down."
    if kind == "server":
        return f"{service} is answering with errors of its own: check its logs."
    return "Look at the last error in SirisAI's scheduler jobs (GET /siris/scheduler/jobs)."


def find(siris: "SirisAI", now: datetime | None = None) -> dict[str, dict[str, Any]]:
    """What's broken right now: {key: {title, body}}."""
    from app.core import scheduler
    from app.core.health import broken_tools
    from app.upkeep import reliability

    now = now or datetime.now(UTC)
    out: dict[str, dict[str, Any]] = {}

    since = now - timedelta(hours=3)
    failures = [f for f in (siris.state.get(reliability.FAILURES_KEY) or [])
                if not f.get("slow") and datetime.fromisoformat(f["at"]) >= since]
    for status in scheduler.job_status():
        name = str(status["name"])
        error = status.get("last_error")
        if not error:
            continue
        count = sum(1 for f in failures if f["job"] == name)
        if count >= MIN_JOB_FAILURES:
            out[f"job:{name}"] = {
                "title": f"The {name.replace('_', ' ')} job keeps failing",
                "body": f"Failed {count} times in the last 3 hours. Last error: {str(error)[:240]}\n\n{advice(name, str(error))}",
            }

    paused = broken_tools(siris, now)
    with siris.audit._session_factory() as session:
        rows = session.execute(
            select(ToolExecution.tool_name, ToolExecution.error, ToolExecution.created_at)
            .where(ToolExecution.created_at >= (now - WINDOW).replace(tzinfo=None))
            .order_by(ToolExecution.created_at)
        ).all()
    by_tool: dict[str, list[tuple[str | None, datetime]]] = {}
    for tool, error, at in rows:
        by_tool.setdefault(tool, []).append((error, at))
    for tool, calls in by_tool.items():
        if tool in IGNORED_TOOLS or tool in paused:
            continue
        bad = [(e, at) for e, at in calls if e and _kind(e)]
        if len(bad) < MIN_TOOL_FAILURES or bad[-1][1] - bad[0][1] < MIN_SPREAD:
            continue
        if any(e is None and at > bad[-1][1] for e, at in calls):
            continue   # it has worked since
        error = bad[-1][0] or ""
        out[f"tool:{tool}"] = {
            "title": f"{_service(tool)} keeps failing",
            "body": f"{tool} failed {len(bad)} times in the last hour. Last error: {error[:240]}\n\n{advice(tool, error)}",
        }
    return out


def _recovered(siris: "SirisAI", key: str, since: datetime, now: datetime) -> bool | None:
    """True: proved working again (a job's last run passed, or a tool succeeded after the alert).
    False: not seen failing for a day, so dropped without a "fixed". None: can't tell yet."""
    from app.core import scheduler

    kind, _, name = key.partition(":")
    if kind == "job":
        status = next((j for j in scheduler.job_status() if j["name"] == name), None)
        return True if status is not None and status.get("runs") and not status.get("last_error") else None
    with siris.audit._session_factory() as session:
        rows = session.execute(
            select(ToolExecution.error, ToolExecution.created_at)
            .where(ToolExecution.tool_name == name, ToolExecution.created_at >= (since - WINDOW).replace(tzinfo=None))
        ).all()
    last_failure = max((at for error, at in rows if error), default=since.replace(tzinfo=None))
    if any(error is None and at > last_failure for error, at in rows):
        return True
    if now.replace(tzinfo=None) - last_failure >= timedelta(days=1):
        return False
    return None


def check(siris: "SirisAI", now: datetime | None = None) -> dict[str, list[str]]:
    """Push new breakages, daily reminders for ongoing ones, and fixes. Returns what it said."""
    from app.upkeep.alerts import raise_alert

    now = now or datetime.now(UTC)
    known: dict[str, dict[str, Any]] = dict(siris.state.get(STATE_KEY) or {})
    current = find(siris, now)
    said: dict[str, list[str]] = {"started": [], "ongoing": [], "fixed": [], "dropped": []}

    for key, problem in current.items():
        entry = known.get(key)
        if entry is None:
            raise_alert(siris, FEED_PREFIX + key, problem["title"], problem["body"], severity="attention", push=True)
            known[key] = {"title": problem["title"], "since": now.isoformat(), "pushed": now.isoformat()}
            said["started"].append(key)
        elif now - datetime.fromisoformat(entry["pushed"]) >= REPEAT_EVERY:
            days = (now - datetime.fromisoformat(entry["since"])).days
            raise_alert(siris, FEED_PREFIX + key, problem["title"],
                        f"Still broken ({days} day{'s' if days != 1 else ''}). {problem['body']}", severity="attention", push=True)
            entry["pushed"] = now.isoformat()
            said["ongoing"].append(key)

    for key in [k for k in known if k not in current]:
        verdict = _recovered(siris, key, datetime.fromisoformat(known[key]["since"]), now)
        if verdict is None:
            continue   # no longer failing, but nothing has proved it works yet (a tool nobody has called)
        title = known.pop(key)["title"]
        try:
            siris.attention.resolve_key(FEED_PREFIX + key, "Working again" if verdict else "Not seen for a day")
        except Exception:  # noqa: BLE001
            logger.debug("Resolving %s failed", key, exc_info=True)
        if verdict and siris.tools.get("notify") is not None:
            try:
                siris.run_tool("notify", {"title": "SirisAI: fixed", "message": f"Working again: {title.replace(' keeps failing', '')}."},
                               trigger="breakage watch")
            except Exception:  # noqa: BLE001
                logger.warning("Pushing a fix for %s failed", key)
        said["fixed" if verdict else "dropped"].append(key)

    siris.state.set(STATE_KEY, known)
    return said
