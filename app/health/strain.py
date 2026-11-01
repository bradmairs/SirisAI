"""Strain (4.03): how hard the body worked, 0 to 21, like Whoop's and Bevel's.

Heart rate against your maximum, minute by minute where it can be, in five
zones weighted 1 to 5 (50-60% of max is 1, 90%+ is 5; under 50% counts half),
added up and squashed onto 0-21 so the first hard hour counts far more than
the third: 60 minutes at 70-80% is about 14. Per workout from its average
heart rate (or its energy when there's none). For the day, the workouts plus
the other hours' average heart rate (Health Auto Export by hour), at a
quarter weight, since daily life is spread over the hour.

**SirisGYM** (4.06): SirisOS sends each day's gym sessions (source
`sirisgym`). One that overlaps a workout from the Watch is left out, since
the Watch's heart rate says it better; one without counts its minutes at a
resistance-training weight (1.5).

Max heart rate is SIRISAI_HEALTH_MAX_HR, else the highest a workout has
reached in six months, else 190. **Target** from this morning's readiness:
Primed 14-18, Steady 10-14, Recover 4-9.
"""

from __future__ import annotations

import math
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any

from app.health import auto_export

if TYPE_CHECKING:
    from app.core.siris import SirisAI

SCALE = 165.0
TARGETS = {"Primed": (14, 18), "Steady": (10, 14), "Recover": (4, 9)}


def zone_weight(hr: float, max_hr: float) -> float:
    share = hr / max_hr
    if share < 0.5:
        return 0.5
    return min(5.0, 1.0 + math.floor((share - 0.5) * 10))


def to_strain(load: float) -> float:
    return round(21 * (1 - math.exp(-max(load, 0.0) / SCALE)), 1)


GYM_WEIGHT = 1.5


def workout_load(w: dict[str, Any], max_hr: float) -> float:
    minutes = float(w.get("minutes") or 0)
    if w.get("avg_hr"):
        return minutes * zone_weight(float(w["avg_hr"]), max_hr)
    if w.get("kcal"):
        return float(w["kcal"]) / 4  # about zone 2-3 for a typical session
    if w.get("source") == "sirisgym":
        return minutes * GYM_WEIGHT
    return minutes * 2


def without_doubles(workouts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A SirisGYM session that overlaps another source's workout (the Watch) is left out."""
    others = [(t, t + timedelta(minutes=float(w.get("minutes") or 30))) for w in workouts
              if w.get("source") != "sirisgym" and (t := auto_export.when(w.get("start")))]
    out = []
    for w in workouts:
        if w.get("source") == "sirisgym" and (t := auto_export.when(w.get("start"))):
            end = t + timedelta(minutes=float(w.get("minutes") or 30))
            if any(a < end and t < b for a, b in others):
                continue
        out.append(w)
    return out


def max_hr(siris: "SirisAI", workouts: list[dict[str, Any]]) -> float:
    if siris.settings.health_max_hr:
        return float(siris.settings.health_max_hr)
    seen = [float(w["max_hr"]) for w in workouts if w.get("max_hr")]
    return max(seen) if seen and max(seen) >= 150 else 190.0


def day_strain(workouts: list[dict[str, Any]], hours: dict[str, dict[str, float]], hr_max: float) -> tuple[float, list[dict[str, Any]]]:
    each = [{"name": w.get("name"), "start": w.get("start"), "minutes": w.get("minutes"),
             "strain": to_strain(workout_load(w, hr_max))} for w in workouts]
    load = sum(workout_load(w, hr_max) for w in workouts)
    busy = {t.hour for w in workouts if (t := auto_export.when(w.get("start")))}
    for hour, bucket in hours.items():
        if bucket.get("hr") and int(hour) not in busy and bucket["hr"] >= 0.5 * hr_max:
            load += 60 * zone_weight(bucket["hr"], hr_max) * 0.25
    return to_strain(load), each


def build(siris: "SirisAI", readiness: dict[str, Any] | None, on: date) -> dict[str, Any] | None:
    from app.health import battery

    history = siris.health.workouts((on - timedelta(days=180)).isoformat())
    hr_max = max_hr(siris, history)
    week = []
    today_each: list[dict[str, Any]] = []
    for i in range(6, -1, -1):
        d = (on - timedelta(days=i)).isoformat()
        ws = without_doubles([w for w in history if w["date"] == d])
        value, each = day_strain(ws, battery.intraday(siris, d), hr_max)
        week.append({"date": d, "strain": value})
        if i == 0:
            today_each = each
    if not any(x["strain"] for x in week) and not today_each:
        return None
    lo, hi = TARGETS.get((readiness or {}).get("band"), (8, 14))
    now = week[-1]["strain"]
    status = "under" if now < lo else "over" if now > hi else "in range"
    return {"today": now, "target": [lo, hi], "status": status, "workouts": today_each, "week": week, "max_hr": hr_max,
            "advice": {"under": f"Room for more: {lo}-{hi} suits today's readiness.",
                       "in range": "Right where today's readiness says it should be.",
                       "over": "Past what today's readiness suggests: go easy for the rest of the day."}[status]}
