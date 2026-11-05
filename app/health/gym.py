"""SirisGym's training, as SirisOS sends it (4.16): after every workout
SirisOS posts a summary (POST /siris/health/gym): recent workouts, each main
lift's estimated 1RM over its last sessions, this week by muscle, records in
the last 30 days, the deload state, and the next routine day with what to
lift. Chat and voice answer from it with the gym_progress tool ("how's my
bench trending", "what should I do today", "am I training legs enough"),
and the weekly Health note gets a line. It's health data: kept off cloud
models when SIRISAI_HEALTH_PRIVATE is on, like the other health tools."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from app.core.user_context import get_current_user_id

if TYPE_CHECKING:
    from app.core.siris import SirisAI


def _key() -> str:
    return f"gym_summary:{get_current_user_id()}"


def keep(siris: "SirisAI", summary: dict[str, Any]) -> None:
    siris.state.set(_key(), {**summary, "received_at": datetime.now(UTC).isoformat(timespec="seconds")})


def latest(siris: "SirisAI") -> dict[str, Any] | None:
    got = siris.state.get(_key())
    return dict(got) if isinstance(got, dict) else None


def trend(name: str, lift: dict[str, Any]) -> str:
    pts = [p for p in lift.get("last") or [] if p.get("e1rm")]
    if not pts:
        return f"{name}: no sessions yet."
    first, last = pts[0], pts[-1]
    change = round(last["e1rm"] - first["e1rm"], 1)
    way = "steady" if abs(change) < 1 else f"up {change:g} kg" if change > 0 else f"down {abs(change):g} kg"
    span = f" over {len(pts)} sessions since {first['date']}" if len(pts) > 1 else ""
    return (f"{name}: estimated 1RM {last['e1rm']:g} kg on {last['date']} (top set {last.get('top') or '?'} kg), {way}{span}; "
            f"best ever {lift.get('best'):g} kg.")


def text(s: dict[str, Any], exercise: str | None = None) -> str:
    lifts: dict[str, Any] = s.get("lifts") or {}
    if exercise:
        want = exercise.lower()
        hit = next((n for n in lifts if want in n.lower() or n.lower().split()[0] in want), None)
        return trend(hit, lifts[hit]) if hit else f"No {exercise} in SirisGym's main lifts yet (it tracks {', '.join(lifts) or 'none so far'})."
    parts = [f"{s.get('total_sessions', 0)} workouts logged, {s.get('streak_weeks', 0)} weeks in a row."]
    week = s.get("week") or {}
    if week:
        parts.append(f"This week: {week.get('sessions', 0)} workouts, {round(week.get('volume') or 0):,} kg lifted.")
    under = [m["muscle"] for m in s.get("muscles") or [] if m.get("status") == "under"]
    over = [m["muscle"] for m in s.get("muscles") or [] if m.get("status") == "over"]
    if under:
        parts.append(f"Under 10-20 sets this week: {', '.join(under[:5])}.")
    if over:
        parts.append(f"Over: {', '.join(over)}.")
    if (d := s.get("deload") or {}).get("active"):
        parts.append(f"In a deload week until {d.get('until')}.")
    elif d.get("suggest"):
        parts.append("A deload week is suggested: " + "; ".join(d.get("why") or []) + ".")
    if s.get("records_30d"):
        parts.append("Records this month: " + "; ".join(s["records_30d"][-4:]) + ".")
    if n := s.get("next"):
        lifts_next = ", ".join(f"{e['name']} {e['sets']}×{e['reps']}" + (f" at {e['weight']:g} kg" if e.get("weight") else "") for e in n.get("exercises") or [])
        when = f" on {n['on']}" if n.get("rest_day") and n.get("on") else ""
        lead = "Today's a rest day. " if n.get("rest_day") else ""
        parts.append(f"{lead}Next{when}: {n['routine']}, {n['day']}: {lifts_next}.")
    parts += [trend(n, lift) for n, lift in list(lifts.items())[:3]]
    return " ".join(parts)


def week_line(siris: "SirisAI") -> str | None:
    s = latest(siris)
    week = (s or {}).get("week") or {}
    if not week.get("sessions"):
        return None
    recs = (s or {}).get("records_30d") or []
    return f"- Gym: {week['sessions']} workouts, {round(week.get('volume') or 0):,} kg lifted" + (f"; latest record: {recs[-1]}" if recs else "")
