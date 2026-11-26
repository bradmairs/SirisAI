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

from app.core import clock

from app.db.models import MetricSample, ToolExecution
from app.upkeep import plain

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
        what = f"failed {plain._times(j['failed'])}" if j["failed"] else f"ran slow {plain._times(j['slow'])}"
        why = f" because {plain.reason(j['last_error'])}" if j["last_error"] else ""
        out["problems"].append(f"The {j['job'].replace('_', ' ')} job {what}{why}."
                               + (f" Fix: {_JOB_ADVICE[j['job']]}." if j["job"] in _JOB_ADVICE else ""))

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
    # 6.73: in plain words, one line per service (the raw errors stay in out["tools"]).
    out["problems"] += plain.summarise([(tool, error) for tool, error in rows
                                        if not any(w in (error or "").lower() for w in ("confirmation", "pin"))])
    try:
        from app.core.health import broken_tools

        out["paused_tools"] = sorted(broken_tools(siris))
    except Exception:  # noqa: BLE001
        out["paused_tools"] = []
    if out["paused_tools"]:
        names = sorted({plain.service(t) for t in out["paused_tools"]})
        out["problems"].append("Paused for now because they're down: " + ", ".join(names) + ".")

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
                               f"{out['speed']['previous_p50_ms'] / 1000:.1f} s.")
    # 7.27: the voice speed target -- something heard within SIRISAI_VOICE_TARGET_MS of finishing speaking.
    out["voice"] = voice_speed(siris, since, now)
    if out["voice"]["turns"] >= 5 and out["voice"]["p50_ms"] > out["voice"]["target_ms"]:
        out["problems"].append(
            f"Spoken replies typically start after {out['voice']['p50_ms'] / 1000:.1f} s (target "
            f"{out['voice']['target_ms'] / 1000:.1f} s); {out['voice']['within_target_percent']}% were on time."
        )
    out["ok"] = not out["problems"]
    return out


def voice_speed(siris: "SirisAI", since: datetime, now: datetime) -> dict[str, Any]:
    first = _metric(siris, "voice_first_audio_ms", since, now)
    answer = _metric(siris, "voice_first_answer_ms", since, now)
    target = siris.settings.voice_target_ms
    return {
        "turns": len(first), "target_ms": target,
        "p50_ms": _percentile(first, 50), "p90_ms": _percentile(first, 90),
        "answer_p50_ms": _percentile(answer, 50),
        "within_target_percent": round(100 * sum(v <= target for v in first) / len(first)) if first else None,
    }


def text(report: dict[str, Any]) -> str:
    """The feed item's body: what went wrong, one line each, then the speed."""
    lines = list(report["problems"]) or ["Nothing failed."]
    lines += report.get("week") or []   # 4.29.0: what broke and what fixed itself
    speed = report["speed"]
    if speed["replies"]:
        lines.append(f"{speed['replies']} replies, typically {speed['p50_ms'] / 1000:.1f} s.")
    voice = report.get("voice") or {}
    if voice.get("turns"):
        lines.append(f"{voice['turns']} spoken replies: first words after {voice['p50_ms'] / 1000:.1f} s typically, "
                     f"{voice['within_target_percent']}% within {voice['target_ms'] / 1000:.1f} s.")
    if report.get("tool_choice"):
        lines.append(f"Picked the right tool {report['tool_choice']['accuracy']}% of the time.")
    return "\n".join(lines)


def weekly(siris: "SirisAI", now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    report = build(siris, 7, now)
    try:
        from app.upkeep import week_review

        report["week"] = week_review.lines(siris, now)
    except Exception:  # noqa: BLE001
        report["week"] = []
    count = len(report["problems"])
    title = "SirisAI this week: all fine" if report["ok"] else f"SirisAI this week: {count} thing{'s' if count != 1 else ''} to fix"
    # One card a week (6.73): keyed by the week, and last week's goes when this one arrives.
    key = f"reliability:{now.astimezone(clock.get_zone(siris.settings.timezone)):%G-W%V}"
    siris.attention.supersede("reliability", key, "Replaced by this week's report")
    siris.attention.raise_item(
        key, "notice" if report["ok"] else "alert", title,
        source="reliability", severity="info" if report["ok"] else "attention", body=text(report),
    )
    if not report["ok"] and siris.tools.get("notify") is not None:
        try:
            siris.run_tool("notify", {"title": title, "message": text(report)[:900]}, trigger="the weekly reliability report")
        except Exception:  # noqa: BLE001
            pass
    return report
