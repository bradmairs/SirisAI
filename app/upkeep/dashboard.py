"""SirisAI's own health, on one page (6.58): GET /siris/upkeep/dashboard,
and the Status tab's "How SirisAI is doing".

- **Replies**: median and 95th-percentile time per stage (context, tools,
  model, total), and whether replies are getting slower (the newest 50
  turns against the 150 before);
- **Tools**: calls and failures in the last 24 hours, the worst ones, and
  any paused for failing;
- **Model**: switches to a fallback Ollama machine in the last 24 hours;
- **Background jobs**: each job's last run, time taken and last error,
  slow ones first;
- **Self-test**: the nightly results over the last 30 nights, and the check
  after the last update.

So a slow drift or a job that keeps failing shows before it breaks.
"""

from __future__ import annotations

import time
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.core.siris import SirisAI

_STARTED = time.time()


def _p50(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[len(ordered) // 2], 1)


def build(siris: "SirisAI", now: datetime | None = None) -> dict[str, Any]:
    from app import __version__
    from app.core import scheduler
    from app.core.health import broken_tools
    from app.cortex import timings
    from app.llm import ollama
    from app.upkeep import release_check, self_test

    now = now or datetime.now(UTC)
    replies = timings.summary(siris)
    totals = list(reversed(siris.metrics.recent_values("turn_total_ms", limit=200)))  # oldest first
    newest, before = totals[-50:], totals[:-50][-150:]
    trend = None
    if len(newest) >= 20 and len(before) >= 50:
        a, b = _p50(before), _p50(newest)
        if a and b:
            trend = {"before_p50_ms": a, "now_p50_ms": b, "change_percent": round((b - a) / a * 100)}

    calls = siris.audit.since(now - timedelta(hours=24), limit=5000)
    failed = Counter(c["tool_name"] for c in calls if c.get("error"))
    used = Counter(c["tool_name"] for c in calls)
    tools = {"calls": len(calls), "failed": sum(failed.values()),
             "worst": [{"tool": t, "failed": n, "calls": used[t]} for t, n in failed.most_common(6)],
             "paused": sorted(broken_tools(siris))}

    cutoff = (now - timedelta(hours=24)).isoformat()
    fallbacks = [f for f in list(ollama.FALLBACKS) if f["at"] >= cutoff]

    jobs = scheduler.job_status()
    jobs.sort(key=lambda j: (not j.get("slow"), not j.get("last_error"), j["name"]))

    history = siris.state.get(self_test.HISTORY_KEY) or []
    checks = {"self_test": history[-30:], "last_self_test": siris.state.get(self_test.RESULTS_KEY),
              "after_update": siris.state.get(release_check.STATE_KEY)}

    problems = []
    if trend and trend["change_percent"] >= 30:
        problems.append(f"Replies are {trend['change_percent']}% slower than before ({trend['now_p50_ms']:.0f} ms against {trend['before_p50_ms']:.0f} ms).")
    if tools["paused"]:
        problems.append(f"Paused for failing: {', '.join(tools['paused'])}.")
    bad = [w for w in tools["worst"] if w["failed"] >= 3]
    if bad:
        problems.append("Failing today: " + ", ".join(f"{w['tool']} ({w['failed']} of {w['calls']})" for w in bad[:3]) + ".")
    if fallbacks:
        problems.append(f"The model switched to a fallback machine {len(fallbacks)} times today.")
    job_errors = [j["name"] for j in jobs if j.get("last_error")]
    if job_errors:
        problems.append(f"Background jobs whose last run failed: {', '.join(job_errors[:5])}.")
    if history and history[-1].get("failed"):
        problems.append(f"The last self-test got {history[-1]['failed']} wrong.")
    return {
        "version": __version__, "up_hours": round((time.time() - _STARTED) / 3600, 1), "at": now.isoformat(),
        "replies": {**replies, "trend": trend}, "tools": tools, "fallbacks": fallbacks[-20:], "jobs": jobs, "checks": checks,
        "problems": problems, "text": " ".join(problems) if problems else "All well: replies steady, no failing tools or jobs.",
    }
