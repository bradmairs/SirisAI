"""Planning the training week (6.58): gym_plan_week.

Takes how often you train (your SirisGym workouts over the last four weeks,
3 a week when there's no history), the next 7 days of the calendar and
this morning's readiness, and picks the days: avoiding an early start
(something before 07:30), a packed day (6+ hours booked), an evening taken
up when you usually train in the evening, and two days running; today is
left out when readiness says recover. Each day says why, and the routine's
next workout goes on the first one.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.core.siris import SirisAI

logger = logging.getLogger(__name__)
EARLY = (7, 30)


def _events(siris: "SirisAI", days: int = 7) -> list[dict[str, Any]]:
    for name, args in (("calendar_upcoming_events", {"days_ahead": days}), ("home_assistant_get_calendar_events", {"days": days})):
        if siris.tools.get(name) is not None:
            try:
                return list(siris.run_tool(name, args) or [])
            except Exception as exc:  # noqa: BLE001
                logger.info("Calendar unavailable for the training plan: %s", exc)
    return []


def _local(siris: "SirisAI", stamp: str) -> datetime | None:
    from app.core import clock

    try:
        when = datetime.fromisoformat(str(stamp))
    except ValueError:
        return None
    zone = clock.get_zone(siris.settings.timezone)
    return when.astimezone(zone) if when.tzinfo else when.replace(tzinfo=zone)


def plan(siris: "SirisAI", on: date | None = None, per_week: int | None = None) -> dict[str, Any]:
    from app.health import gym, gym_sessions, gym_week, overview

    on = on or overview.today(siris)
    history, _ = gym_sessions.find(siris, on - timedelta(days=28), on - timedelta(days=1))
    sessions = per_week or (round(len(history) / 4) if history else 3)
    sessions = max(1, min(sessions, 6))
    evening = sum(1 for s in history if (t := _local(siris, s.get("started_at") or "")) and t.hour >= 17) > len(history) / 2 if history else False

    days = {on + timedelta(days=i): {"early": None, "booked_h": 0.0, "evening": None} for i in range(7)}
    for ev in _events(siris):
        start = _local(siris, ev.get("start") or "")
        if start is None or start.date() not in days or ev.get("all_day"):
            continue
        end = _local(siris, ev.get("end") or "") or start + timedelta(hours=1)
        d = days[start.date()]
        if (start.hour, start.minute) < EARLY and (d["early"] is None or start < d["early"][0]):
            d["early"] = (start, ev.get("summary") or "something")
        d["booked_h"] += max(0.0, (end - start).total_seconds() / 3600)
        if 17 <= start.hour < 20:
            d["evening"] = ev.get("summary") or "something"
    ready = gym_week.readiness_today(siris, on)

    scored = []
    for day, d in days.items():
        score, why = 0.0, []
        if d["early"]:
            score -= 3
            why.append(f"an early start ({d['early'][1]} at {d['early'][0]:%H:%M})")
        if d["booked_h"] >= 6:
            score -= 2
            why.append(f"{d['booked_h']:.0f} hours booked")
        if evening and d["evening"]:
            score -= 2
            why.append(f"the evening's taken ({d['evening']})")
        if day == on and ready and ready.get("band") == "Recover":
            score -= 4
            why.append(f"readiness is {ready.get('score')} (recover)")
        scored.append((score, day, why))

    order = sorted(scored, key=lambda t: (-t[0], t[1]))
    picked: list[date] = []
    for _, day, _ in order:  # best days first, not two running
        if len(picked) < sessions and not any(abs((day - p).days) == 1 for p in picked):
            picked.append(day)
    for _, day, _ in order:  # more sessions than spaced days: fill with the next best
        if len(picked) < sessions and day not in picked:
            picked.append(day)
    picked.sort()
    nxt = (gym.latest(siris) or {}).get("next") or {}
    first_name = " ".join(x for x in (nxt.get("routine"), nxt.get("day")) if x)
    out_days = []
    for day in picked:
        reasons = next(w for _, dd, w in scored if dd == day)
        out_days.append({"date": day.isoformat(), "day": day.strftime("%a %-d %b"), "watch_out": reasons})
    skipped = [{"date": dd.isoformat(), "day": dd.strftime("%a %-d %b"), "why": w} for s, dd, w in sorted(scored, key=lambda t: t[1]) if dd not in picked and w]
    text = f"{sessions} workout{'s' if sessions != 1 else ''} this week: " + ", ".join(
        d["day"] + (f" ({first_name})" if i == 0 and first_name else "") + (f", though {'; '.join(d['watch_out'])}" if d["watch_out"] else "")
        for i, d in enumerate(out_days)) + "."
    if skipped:
        text += " Avoiding " + "; ".join(f"{s['day']}: {', '.join(s['why'])}" for s in skipped[:4]) + "."
    return {"sessions": sessions, "days": out_days, "avoiding": skipped, "evening_trainer": evening, "text": text}
