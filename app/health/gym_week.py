"""Training in the morning brief (6.38).

- **Mondays**: last week's training: workouts, working sets, records, the
  muscles under or over their weekly target (from SirisGym's summary), and
  the one change gym_coach.weekly suggests.
- **A low-readiness training day**: when this morning's readiness is in
  "Recover" and SirisGym has a workout planned today, a lighter version of
  it: a third fewer sets (at least one) and about 10% lighter, rounded to
  2.5 kg. gym_progress says the same when asked.

Both are health data: the brief carries health already, and with
SIRISAI_HEALTH_PRIVATE on it is only composed locally.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.core.siris import SirisAI

logger = logging.getLogger(__name__)
LIGHTER = 0.9


def _round(weight: float, step: float = 2.5) -> float:
    return round(round(weight / step) * step, 2)


def readiness_today(siris: "SirisAI", on: date | None = None) -> dict[str, Any] | None:
    from app.health import overview

    on = on or overview.today(siris)
    try:
        return overview.morning_readiness(siris, on, overview.days(siris, on))
    except Exception:  # noqa: BLE001 - no health data yet
        logger.debug("No readiness for %s", on, exc_info=True)
        return None


def lighter_plan(siris: "SirisAI", readiness: dict[str, Any] | None = None, on: date | None = None) -> dict[str, Any] | None:
    """Today's planned workout, lighter, when readiness says recover. None otherwise."""
    from app.health import gym

    summary = gym.latest(siris) or {}
    nxt = summary.get("next") or {}
    if not nxt.get("exercises") or nxt.get("rest_day"):
        return None
    ready = readiness if readiness is not None else readiness_today(siris, on)
    if not ready or ready.get("band") != "Recover":
        return None
    exercises = []
    for e in nxt["exercises"]:
        try:
            sets = int(e.get("sets") or 0)
        except (TypeError, ValueError):
            sets = 0
        fewer = max(1, sets - max(1, round(sets / 3))) if sets else None
        weight = _round(float(e["weight"]) * LIGHTER) if e.get("weight") else None
        exercises.append({**e, "sets": fewer, "weight": weight, "was": {"sets": e.get("sets"), "weight": e.get("weight")}})
    day = " ".join(x for x in (nxt.get("routine"), nxt.get("day")) if x) or "today's workout"
    parts = [f"{e['name']} {e['sets'] or e['was']['sets']}×{e.get('reps')}" + (f" at {e['weight']:g} kg" if e.get("weight") else "") for e in exercises]
    text = (f"Readiness is {ready.get('score')} (recover), so a lighter {day}: a third fewer sets and about 10% lighter: "
            + ", ".join(parts) + ". Or swap it for a walk and mobility.")
    return {"readiness": ready.get("score"), "day": day, "exercises": exercises, "text": text}


def weekly_summary(siris: "SirisAI", on: date | None = None) -> dict[str, Any] | None:
    """Last Monday-Sunday's training, and one change for this week."""
    from app.health import gym, gym_coach, gym_sessions, overview

    on = on or overview.today(siris)
    start = on - timedelta(days=on.weekday() + 7)
    end = start + timedelta(days=6)
    found, source = gym_sessions.find(siris, start, end)
    if not found:
        return None
    sets, records = 0, []
    for s in found:
        full = gym_sessions.detail(siris, s, source)
        sets += sum(1 for e in full.get("entries") or [] for x in e.get("sets") or [] if x.get("done", True) and x.get("type") != "warmup")
        records += [r.get("text") for r in full.get("records") or [] if r.get("text")]
    summary = gym.latest(siris) or {}
    under = [m["muscle"] for m in summary.get("muscles") or [] if m.get("status") == "under"]
    over = [m["muscle"] for m in summary.get("muscles") or [] if m.get("status") == "over"]
    try:
        change = (gym_coach.weekly(siris, on).get("changes") or [None])[0]
    except Exception:  # noqa: BLE001
        change = None
    text = f"Training last week: {len(found)} workout{'s' if len(found) != 1 else ''}, {sets} working sets"
    text += f", {len(records)} record{'s' if len(records) != 1 else ''} ({'; '.join(records[:3])})" if records else ", no records"
    text += "."
    if under:
        text += f" Under target: {', '.join(under[:4])}."
    if over:
        text += f" Over: {', '.join(over[:3])}."
    if change:
        text += f" This week: {change['title']}."
    return {"from": start.isoformat(), "to": end.isoformat(), "workouts": len(found), "sets": sets, "records": records,
            "under": under, "over": over, "change": change, "text": text}


def briefing_line(siris: "SirisAI", on: date | None = None) -> str | None:
    from app.health import overview

    on = on or overview.today(siris)
    lines = []
    if on.weekday() == 0:
        week = weekly_summary(siris, on)
        if week:
            lines.append(week["text"])
    lighter = lighter_plan(siris, on=on)
    if lighter:
        lines.append(lighter["text"])
    return " ".join(lines) or None


def month_training(siris: "SirisAI", first: date, last: date) -> dict[str, Any] | None:
    """6.58, for the monthly report: SirisGym's workouts that month, sets, records,
    and how each lift trained three times or more moved (first to last session)."""
    from app.health import gym_sessions

    found, source = gym_sessions.find(siris, first, last)
    if not found:
        return None
    sets, records, lifts = 0, [], {}
    for s in sorted(found, key=lambda s: str(s.get("started_at"))):
        full = gym_sessions.detail(siris, s, source)
        records += [r.get("text") for r in full.get("records") or [] if r.get("text")]
        for e in full.get("entries") or []:
            done = [x for x in e.get("sets") or [] if x.get("done", True) and x.get("type") != "warmup"]
            sets += len(done)
            best = gym_sessions._best(e)["e1rm"]
            if best:
                lifts.setdefault(e.get("name") or e.get("exercise") or "?", []).append(best)
    moved = sorted(((name, v[0], v[-1]) for name, v in lifts.items() if len(v) >= 3), key=lambda t: -(t[2] - t[1]))
    lines = [f"- SirisGym: {len(found)} workouts, {sets} working sets, {len(records)} records"]
    lines += [f"- {name}: estimated 1RM {a:g} → {b:g} kg ({b - a:+.1f})" for name, a, b in moved[:6]]
    lines += [f"- Record: {r}" for r in records[:5]]
    return {"workouts": len(found), "sets": sets, "records": records, "lifts": [{"name": n, "from": a, "to": b} for n, a, b in moved],
            "lines": lines}
