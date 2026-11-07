"""The weekly reliability report (SirisAI 5.0).

Sundays at SIRISAI_RELIABILITY_HOUR_LOCAL (20; blank = off), one item in
the attention feed (and a push when something's wrong) saying how SirisAI
itself held up over the week:

- **background jobs** that failed or ran slow, how often, and their last
  error (the scheduler writes each one down as it happens);
- **tools** that errored, the commonest error for each, and the ones paused
  right now because their service is down;
- **choosing tools**: the latest tool-choice accuracy and how it moved;
- **speed**: typical and slowest replies this week against last week, and
  how many were quick commands;
- **what to do** about each problem, in a line.

GET /siris/reliability gives the same for any number of days.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from app.db.models import MetricSample, ToolExecution

if TYPE_CHECKING:
    from app.core.siris import SirisAI

FAILURES_KEY = "reliability:job_failures"
KEEP = 300
_JOB_ADVICE = {
    "servers": "check SirisAI can still reach each server over SSH (SIRISAI_SSH_HOSTS)",
    "devices": "check the UniFi controller is up and its key still works",
    "parcels": "check the parcel tracker's key",
    "security": "look at the security watch's last error",
    "image_scan": "check Docker's socket is mounted and Trivy can pull its database",
    "speed_test": "the speed test server may have changed; it's harmless if it's only now and then",
    "conversation_summaries": "check the local model is running in Ollama",
    "warm_model": "check Ollama is running and SIRISAI_OLLAMA_MODEL is pulled",
    "health_checks": "check Health Auto Export is still sending",
}


def note_job_failure(siris: "SirisAI", name: str, error: str, slow: bool = False) -> None:
    failures = list(siris.state.get(FAILURES_KEY) or [])
    failures.append({"at": datetime.now(UTC).isoformat(), "job": name, "error": error[:300], "slow": slow})
    siris.state.set(FAILURES_KEY, failures[-KEEP:])


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, round(pct / 100 * (len(ordered) - 1)))], 1)


def _metric(siris: "SirisAI", name: str, start: datetime, end: datetime) -> list[float]:
    with siris.metrics._session_factory() as session:
        return [v for (v,) in session.execute(
            select(MetricSample.value).where(
                MetricSample.metric_name == name,
                MetricSample.created_at >= start.astimezone(UTC).replace(tzinfo=None),
                MetricSample.created_at < end.astimezone(UTC).replace(tzinfo=None),
            )
        ).all()]


def build(siris: "SirisAI", days: int = 7, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    since = now - timedelta(days=days)
    out: dict[str, Any] = {"days": days, "since": since.isoformat(), "problems": []}

    jobs: dict[str, dict[str, Any]] = {}
    for f in siris.state.get(FAILURES_KEY) or []:
        if datetime.fromisoformat(f["at"]) < since:
            continue
        j = jobs.setdefault(f["job"], {"job": f["job"], "failed": 0, "slow": 0, "last_error": None})
        j["slow" if f.get("slow") else "failed"] += 1
        if not f.get("slow"):
            j["last_error"] = f["error"]
    out["jobs"] = sorted(jobs.values(), key=lambda j: -(j["failed"] + j["slow"]))
    for j in out["jobs"]:
        what = f"{j['failed']} failure{'s' if j['failed'] != 1 else ''}" if j["failed"] else f"slow {j['slow']} time{'s' if j['slow'] != 1 else ''}"
        out["problems"].append(f"The {j['job'].replace('_', ' ')} job: {what}"
                               + (f" (last: {j['last_error']})" if j["last_error"] else "")
                               + f". Fix: {_JOB_ADVICE.get(j['job'], 'look at its last error in GET /siris/scheduler/jobs')}.")

    with siris.audit._session_factory() as session:
        rows = session.execute(
            select(ToolExecution.tool_name, ToolExecution.error).where(
                ToolExecution.created_at >= since.astimezone(UTC).replace(tzinfo=None), ToolExecution.error.is_not(None)
            )
        ).all()
    errors: dict[str, Counter] = {}
    for tool, error in rows:
        if "confirmation" in (error or "").lower() or "pin" in (error or "").lower():
            continue
        errors.setdefault(tool, Counter())[(error or "")[:160]] += 1
    out["tools"] = sorted(
        ({"tool": t, "errors": sum(c.values()), "commonest": c.most_common(1)[0][0]} for t, c in errors.items()),
        key=lambda t: -t["errors"],
    )[:8]
    for t in out["tools"][:5]:
        out["problems"].append(f"{t['tool']} failed {t['errors']} time{'s' if t['errors'] != 1 else ''}, mostly: {t['commonest']}.")
    try:
        from app.core.health import broken_tools

        out["paused_tools"] = sorted(broken_tools(siris))
    except Exception:  # noqa: BLE001
        out["paused_tools"] = []
    if out["paused_tools"]:
        out["problems"].append("Paused now because their service is down: " + ", ".join(out["paused_tools"]) + ".")

    from app.evals import tool_choice

    history = siris.state.get(tool_choice.HISTORY_KEY) or []
    if history:
        latest = history[-1]
        out["tool_choice"] = {"accuracy": latest.get("choice_accuracy"), "previous": history[-2].get("choice_accuracy") if len(history) > 1 else None}
        if out["tool_choice"]["previous"] is not None and out["tool_choice"]["accuracy"] < out["tool_choice"]["previous"] - 5:
            out["problems"].append(f"Picking the right tool fell from {out['tool_choice']['previous']}% to {out['tool_choice']['accuracy']}%: "
                                   "a model change or a new tool with an overlapping description is the usual cause.")

    this_week = _metric(siris, "turn_total_ms", since, now)
    last_week = _metric(siris, "turn_total_ms", since - timedelta(days=days), since)
    out["speed"] = {
        "replies": len(this_week), "p50_ms": _percentile(this_week, 50), "p95_ms": _percentile(this_week, 95),
        "previous_p50_ms": _percentile(last_week, 50),
    }
    if out["speed"]["p50_ms"] and out["speed"]["previous_p50_ms"] and out["speed"]["p50_ms"] > 1.3 * out["speed"]["previous_p50_ms"]:
        out["problems"].append(f"Replies got slower: typically {out['speed']['p50_ms'] / 1000:.1f} s, up from "
                               f"{out['speed']['previous_p50_ms'] / 1000:.1f} s. GET /siris/timings shows which stage.")
    out["ok"] = not out["problems"]
    return out


def text(report: dict[str, Any]) -> str:
    speed = report["speed"]
    lines = []
    if speed["replies"]:
        lines.append(f"{speed['replies']} replies, typically {speed['p50_ms'] / 1000:.1f} s (slowest 5%: {speed['p95_ms'] / 1000:.1f} s).")
    if report.get("tool_choice"):
        lines.append(f"Tool choice {report['tool_choice']['accuracy']}% right.")
    lines += report["problems"] or ["Nothing failed."]
    return " ".join(lines)


def weekly(siris: "SirisAI", now: datetime | None = None) -> dict[str, Any]:
    report = build(siris, 7, now)
    title = "SirisAI this week: all fine" if report["ok"] else f"SirisAI this week: {len(report['problems'])} thing{'s' if len(report['problems']) != 1 else ''} to fix"
    siris.attention.raise_item(
        f"reliability:{(now or datetime.now(UTC)).date().isoformat()}", "notice" if report["ok"] else "alert", title,
        source="reliability", severity="info" if report["ok"] else "attention", body=text(report),
    )
    if not report["ok"] and siris.tools.get("notify") is not None:
        try:
            siris.run_tool("notify", {"title": title, "message": text(report)[:900]}, trigger="the weekly reliability report")
        except Exception:  # noqa: BLE001
            pass
    return report
