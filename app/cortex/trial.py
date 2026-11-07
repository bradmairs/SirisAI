"""The speed trial (SirisAI 5.0): did 4.6's speed changes actually help on
this server?

Started from SirisOS (System status → SirisAI speed) or POST
/siris/timings/trial, it runs for N days (14 by default), alternating:

- **new** days: as 4.6 does it (the model kept warm in the warm hours, and
  the changing context just before the newest message);
- **old** days: as before 4.6 (no warm-up, context at the top).

Every reply's stage times are recorded under that day's arm as well as
overall. When the trial ends, the attention feed gets the result: typical
and slow times for the whole reply, the first word and the model on each
kind of day, whether the change is worth keeping, and warm hours fitted to
when you actually ask things (the hours that hold almost all your
questions). GET /siris/timings/trial shows it while it runs.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from app.core import clock
from app.db.models import MetricSample

if TYPE_CHECKING:
    from app.core.siris import SirisAI

KEY = "speed:trial"
STAGES = ("total", "first_word", "model")


def active(siris: "SirisAI", now: datetime | None = None) -> dict[str, Any] | None:
    trial = siris.state.get(KEY) or None
    if not trial:
        return None
    now = now or datetime.now(UTC)
    return trial if now < datetime.fromisoformat(trial["ends"]) else None


def arm(siris: "SirisAI", now: datetime | None = None) -> str | None:
    """"new" or "old" for today while a trial runs, else None (4.6 as normal)."""
    trial = active(siris, now)
    if trial is None:
        return None
    zone = clock.get_zone(siris.settings.timezone)
    day = (now or datetime.now(UTC)).astimezone(zone).date().toordinal()
    return "new" if (day - trial["first_day"]) % 2 == 0 else "old"


def start(siris: "SirisAI", days: int = 14, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    days = max(2, min(int(days), 60))
    trial = {
        "started": now.isoformat(), "ends": (now + timedelta(days=days)).isoformat(), "days": days,
        "first_day": now.astimezone(clock.get_zone(siris.settings.timezone)).date().toordinal(),
    }
    siris.state.set(KEY, trial)
    siris.attention.resolve_key("speed:trial-result", "A new trial started")
    return trial


def stop(siris: "SirisAI") -> None:
    siris.state.set(KEY, {})


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, round(pct / 100 * (len(ordered) - 1)))], 1)


def _values(siris: "SirisAI", name: str, since: datetime) -> list[tuple[float, datetime]]:
    with siris.metrics._session_factory() as session:
        rows = session.execute(
            select(MetricSample.value, MetricSample.created_at)
            .where(MetricSample.metric_name == name, MetricSample.created_at >= since.astimezone(UTC).replace(tzinfo=None))
        ).all()
    return [(v, at) for v, at in rows]


def warm_hours(siris: "SirisAI", days: int = 30, now: datetime | None = None) -> str | None:
    """The local hours that hold 95% of the last month's questions, as
    "start-end" for SIRISAI_OLLAMA_WARM_HOURS, or None with too few."""
    now = now or datetime.now(UTC)
    zone = clock.get_zone(siris.settings.timezone)
    hours = [at.replace(tzinfo=UTC).astimezone(zone).hour for _, at in _values(siris, "turn_total_ms", now - timedelta(days=days))]
    if len(hours) < 20:
        return None
    counts = [hours.count(h) for h in range(24)]
    # The tightest window (it may wrap past midnight) holding 95% of them.
    best: tuple[int, int] | None = None
    for length in range(1, 25):
        for first in range(24):
            if sum(counts[(first + i) % 24] for i in range(length)) >= 0.95 * len(hours):
                best = (first, (first + length) % 24)
                break
        if best:
            break
    assert best is not None
    return f"{best[0]}-{best[1] or 24}"


def report(siris: "SirisAI", now: datetime | None = None) -> dict[str, Any]:
    trial = siris.state.get(KEY) or {}
    out: dict[str, Any] = {"running": active(siris, now) is not None, "trial": trial or None, "arm_today": arm(siris, now)}
    if not trial:
        out["suggested_warm_hours"] = warm_hours(siris, now=now)
        return out
    since = datetime.fromisoformat(trial["started"])
    arms: dict[str, Any] = {}
    for which in ("new", "old"):
        arms[which] = {}
        for stage in STAGES:
            values = [v for v, _ in _values(siris, f"turn_{stage}_ms@{which}", since)]
            arms[which][stage] = {"p50_ms": _percentile(values, 50), "p95_ms": _percentile(values, 95), "count": len(values)}
    out["arms"] = arms
    new, old = arms["new"]["total"]["p50_ms"], arms["old"]["total"]["p50_ms"]
    enough = min(arms["new"]["total"]["count"], arms["old"]["total"]["count"]) >= 10
    if not enough or new is None or old is None:
        out["verdict"] = "Not enough replies on both kinds of day yet to tell."
    elif new <= old * 0.9:
        out["verdict"] = f"Keep it: replies are typically {old - new:,.0f} ms ({100 * (old - new) / old:.0f}%) faster with the speed changes."
    elif new >= old * 1.1:
        out["verdict"] = f"The speed changes made replies slower here ({new - old:,.0f} ms). Turn off the warm-up (SIRISAI_OLLAMA_WARM_HOURS blank) and tell me."
    else:
        out["verdict"] = "No clear difference: the changes are harmless here, but they aren't what makes replies slow."
    out["suggested_warm_hours"] = warm_hours(siris, now=now)
    return out


def finish_if_due(siris: "SirisAI", now: datetime | None = None) -> dict[str, Any] | None:
    """Once a trial's days are up: report it in the attention feed, then clear it."""
    trial = siris.state.get(KEY) or {}
    now = now or datetime.now(UTC)
    if not trial or now < datetime.fromisoformat(trial["ends"]):
        return None
    result = report(siris, now)
    lines = [result["verdict"]]
    for which in ("new", "old"):
        total = result["arms"][which]["total"]
        if total["count"]:
            lines.append(f"{'With' if which == 'new' else 'Without'} the changes: typically {total['p50_ms']:,.0f} ms, slowest 5% {total['p95_ms']:,.0f} ms ({total['count']} replies).")
    if result.get("suggested_warm_hours"):
        lines.append(f"You ask most things between {result['suggested_warm_hours']}: set SIRISAI_OLLAMA_WARM_HOURS to that.")
    siris.attention.raise_item("speed:trial-result", "notice", "Speed trial finished", source="speed", severity="info", body=" ".join(lines))
    siris.state.set(KEY, {})
    return result
