"""The energy score (3.62): a Body Battery for the day, alongside readiness.

Readiness is the morning's verdict on the night and stays put. Energy starts
there and moves with the day:
- **Charge at waking**: 25 + 0.75 x readiness (readiness 100 starts you at
  100, 60 at 70), or 70 with no readiness.
- **Being awake** drains 1.6 an hour.
- **Activity** drains with active energy: about 5.5 per 100 kcal, so a 600
  kcal run costs 33.
- **Stress**: an hour whose average heart rate sits more than 15 bpm over
  your resting rate while you're barely moving (under 60 kcal) drains up to
  4 more.
- **Rest** puts a little back: a still hour near your resting heart rate
  (+1.2), and mindful minutes (+0.15 each).
Hourly figures come from Health Auto Export when it's set to aggregate by
hour (or not at all); they're kept for three days in app state. Exported by
day, there are no hours to go on, so it's estimated from the day's active
energy and workouts so far (`estimated`).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any

from app.core.user_context import get_current_user_id
from app.health import auto_export, scores

if TYPE_CHECKING:
    from app.core.siris import SirisAI

AWAKE_DRAIN = 1.6
KCAL_DRAIN = 0.055
REST_GAIN = 1.2
MINDFUL_GAIN = 0.15


def _key() -> str:
    return f"health_intraday:{get_current_user_id()}"


def keep_intraday(siris: "SirisAI", intraday: dict[str, dict[str, dict[str, float]]], keep_days: int = 3) -> None:
    """Merge an export's hourly buckets in: an hour sent again replaces itself."""
    if not intraday:
        return
    kept = dict(siris.state.get(_key()) or {})
    for day, hours in intraday.items():
        merged = dict(kept.get(day) or {})
        for hour, values in hours.items():
            merged[hour] = {**(merged.get(hour) or {}), **values}
        kept[day] = merged
    for day in sorted(kept)[:-keep_days]:
        kept.pop(day)
    siris.state.set(_key(), kept)


def intraday(siris: "SirisAI", day: str) -> dict[str, dict[str, float]]:
    return dict((siris.state.get(_key()) or {}).get(day) or {})


def start_charge(readiness: dict[str, Any] | None) -> float:
    return scores.clamp(25 + 0.75 * readiness["score"]) if readiness else 70.0


def compute(readiness: dict[str, Any] | None, hours: dict[str, dict[str, float]], wake: datetime, now: datetime,
            resting_hr: float | None, workouts: list[dict[str, Any]] | None = None, day_kcal: float | None = None) -> dict[str, Any]:
    start = start_charge(readiness)
    level = start
    spent = {"awake": 0.0, "activity": 0.0, "stress": 0.0}
    regained = 0.0
    timeline = [{"time": wake.strftime("%H:%M"), "value": round(level)}]
    estimated = not any(h.get("kcal") is not None or h.get("hr") is not None for h in hours.values())
    by_hour_kcal: dict[int, float] = {}
    if estimated:
        # No hours: put each workout's energy at its start, and the rest of
        # the day's active energy evenly over the hours awake so far.
        for w in workouts or []:
            t = auto_export.when(w.get("start"))
            if t and wake <= t <= now:
                by_hour_kcal[t.hour] = by_hour_kcal.get(t.hour, 0) + float(w.get("kcal") or (w.get("minutes") or 0) * 8)
        awake_h = max((now - wake).total_seconds() / 3600, 1)
        rest = max(float(day_kcal or 0) - sum(by_hour_kcal.values()), 0)
        for h in range(wake.hour, now.hour + 1):
            by_hour_kcal[h] = by_hour_kcal.get(h, 0) + rest / awake_h
    t = wake
    while t < now:
        step_end = min(t.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1), now)
        share = (step_end - t).total_seconds() / 3600
        bucket = hours.get(f"{t.hour:02d}") or {}
        kcal = (bucket.get("kcal") if not estimated else by_hour_kcal.get(t.hour, 0)) or 0
        hr = bucket.get("hr")
        awake = AWAKE_DRAIN * share
        activity = kcal * KCAL_DRAIN * share
        stress = 0.0
        gain = 0.0
        if hr and resting_hr:
            if kcal < 60 and hr > resting_hr + 15:
                stress = min(4.0, (hr - resting_hr - 15) * 0.2) * share
            elif kcal < 15 and hr <= resting_hr + 6:
                gain += REST_GAIN * share
        gain += (bucket.get("mindful") or 0) * MINDFUL_GAIN * share
        spent["awake"] += awake
        spent["activity"] += activity
        spent["stress"] += stress
        regained += gain
        level = scores.clamp(level - awake - activity - stress + gain)
        t = step_end
        timeline.append({"time": t.strftime("%H:%M"), "value": round(level)})
    now_v = round(level)
    if now_v >= 60:
        summary = "Plenty in the tank."
    elif now_v >= 30:
        summary = "Pace yourself: keep the rest of the day steady."
    else:
        summary = "Running low: an easy evening and an early night will refill it."
    biggest = max(spent, key=lambda k: spent[k])
    if spent[biggest] >= 10 and biggest != "awake":
        summary += {"activity": " Training took the most today.", "stress": " Stress took more than training today."}[biggest]
    return {"now": now_v, "start": round(start), "wake": wake.strftime("%H:%M"), "timeline": timeline[-24:],
            "spent": {k: round(v) for k, v in spent.items()}, "regained": round(regained), "summary": summary, "estimated": estimated}


def today(siris: "SirisAI", readiness: dict[str, Any] | None, data: scores.Days, on: date, now: datetime) -> dict[str, Any] | None:
    """Energy for `on` as of `now` (local, naive). None before waking or with no data at all."""
    if not data.get(on.isoformat()) and not readiness:
        return None
    sleep = (siris.health.sleep_times(on.isoformat()) or {}).get(on.isoformat()) or {}
    wake = auto_export.when(sleep.get("end"))
    if wake is None or wake.date() != on:
        wake = datetime.combine(on, datetime.min.time()).replace(hour=7)
    if now <= wake:
        return None
    rhr = (data.get(on.isoformat()) or {}).get("resting_heart_rate")
    if rhr is None:
        base = scores.baseline(data, "resting_heart_rate", on)
        rhr = base[0] if base else None
    workouts = [w for w in siris.health.workouts(on.isoformat()) if w["date"] == on.isoformat()]
    return compute(readiness, intraday(siris, on.isoformat()), wake, now, rhr, workouts,
                   (data.get(on.isoformat()) or {}).get("active_energy_kcal"))
