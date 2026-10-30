"""The one reader for health data from Apple Health (SirisAI 3.51).

Formats:
- **Health Auto Export's JSON** (the iPhone app's "REST API" automation):
  {"data": {"metrics": [...], "workouts": [...]}}. Each metric has a `name`,
  `units` and `data` (readings with a `date` like "2026-10-28 00:00:00
  +1100" and a `qty`). With "Aggregate data: Day" there's one reading a
  day; without it there are many, combined per day the way each adds up:
  counts and energy summed, rates and heart figures averaged, body
  measurements the last of the day. Sleep (`sleep_analysis`) gives hours,
  the stages and when it started and ended; sleep belongs to the morning
  you woke up. Workouts give time, distance, energy and heart rate, and
  runs add up to `running_km`.
- **SirisAI's own shape**, {"metrics": {day: {metric: value}}, "sleep":
  {day: {start, end}}, "workouts": {day: [...]}}, which is what SirisOS
  kept before 5.8; it sends its history across in this form once.

The day is the reading's own local date, as the phone wrote it. Units are
converted to kg, km, kcal, °C and percent. SirisOS used to keep its own
copies of this reader; since 5.8 it passes the raw export here instead.
"""

from __future__ import annotations

from collections import defaultdict
from statistics import mean
from typing import Any

# Health Auto Export name -> (metric, how a day adds up)
METRICS: dict[str, tuple[str, str]] = {
    "step_count": ("steps", "sum"),
    "active_energy": ("active_energy_kcal", "sum"),
    "basal_energy_burned": ("basal_energy_kcal", "sum"),
    "apple_exercise_time": ("exercise_minutes", "sum"),
    "apple_stand_hour": ("stand_hours", "sum"),
    "walking_running_distance": ("walk_run_km", "sum"),
    "flights_climbed": ("flights", "sum"),
    "mindful_minutes": ("mindful_minutes", "sum"),
    "time_in_daylight": ("daylight_minutes", "sum"),
    "dietary_water": ("water_ml", "sum"),
    "dietary_caffeine": ("caffeine_mg", "sum"),
    "dietary_energy": ("dietary_kcal", "sum"),
    "protein": ("protein_g", "sum"),
    "resting_heart_rate": ("resting_heart_rate", "mean"),
    "heart_rate_variability": ("heart_rate_variability", "mean"),
    "heart_rate_variability_sdnn": ("heart_rate_variability", "mean"),
    "walking_heart_rate_average": ("walking_heart_rate", "mean"),
    "respiratory_rate": ("respiratory_rate", "mean"),
    "blood_oxygen_saturation": ("blood_oxygen", "mean"),
    "oxygen_saturation": ("blood_oxygen", "mean"),
    "apple_sleeping_wrist_temperature": ("wrist_temperature", "mean"),
    "vo2_max": ("vo2_max", "last"),
    "weight_body_mass": ("weight_kg", "last"),
    "body_mass": ("weight_kg", "last"),
    "body_fat_percentage": ("body_fat_pct", "last"),
    "body_mass_index": ("bmi", "last"),
    "lean_body_mass": ("lean_mass_kg", "last"),
}
SLEEP_FIELDS = {"totalSleep": "sleep_hours", "asleep": "sleep_hours", "deep": "sleep_deep_h", "rem": "sleep_rem_h",
                "core": "sleep_core_h", "awake": "sleep_awake_h", "inBed": "in_bed_h"}
# How each metric adds up within a day (for the ones not listed above).
COMBINE: dict[str, str] = {metric: how for metric, how in METRICS.values()}
COMBINE.update({m: "sum" for m in SLEEP_FIELDS.values()})
COMBINE.update({"heart_rate_min": "min", "heart_rate_max": "max", "heart_rate_avg": "mean", "running_km": "sum"})


def looks_like(body: Any) -> bool:
    data = body.get("data") if isinstance(body, dict) else None
    return isinstance(data, dict) and (isinstance(data.get("metrics"), list) or isinstance(data.get("workouts"), list))


def looks_native(body: Any) -> bool:
    return isinstance(body, dict) and isinstance(body.get("metrics"), dict) and all(
        isinstance(v, dict) for v in body["metrics"].values()
    ) and any(len(k) == 10 and k[4] == "-" for k in body["metrics"])


def _day(value: Any) -> str | None:
    text = str(value or "").strip()
    return text[:10] if len(text) >= 10 and text[4] == "-" and text[7] == "-" else None


def _num(value: Any) -> float | None:
    if isinstance(value, dict):
        value = value.get("qty")
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def convert(metric: str, units: str, qty: float) -> float:
    u = units.lower()
    if metric.endswith("_kcal") and u in ("kj", "kilojoule", "kilojoules"):
        return qty / 4.184
    if metric in ("weight_kg", "lean_mass_kg") and u in ("lb", "lbs"):
        return qty * 0.45359237
    if metric == "walk_run_km" and u in ("mi", "miles"):
        return qty * 1.609344
    if metric == "water_ml" and u in ("l", "litre", "liter"):
        return qty * 1000
    if metric == "water_ml" and u in ("fl_oz_us", "fl oz", "floz"):
        return qty * 29.5735
    if metric in ("blood_oxygen", "body_fat_pct") and qty <= 1.0:
        return qty * 100
    if metric == "wrist_temperature" and u in ("degf", "°f", "f"):
        return (qty - 32) * 5 / 9
    return qty


def combine(metric: str, values: list[float]) -> float:
    how = COMBINE.get(metric, "mean")
    v = sum(values) if how == "sum" else min(values) if how == "min" else max(values) if how == "max" else values[-1] if how == "last" else mean(values)
    return round(v, 2)


def workout_entry(name: Any, start: Any, duration_s: float | None, km: float | None, kcal: float | None,
                  avg_hr: float | None = None, max_hr: float | None = None) -> dict[str, Any]:
    minutes = round(duration_s / 60) if duration_s and duration_s > 300 else round(duration_s) if duration_s else None
    return {"name": str(name or "Workout")[:60], "start": str(start or "")[:25], "minutes": minutes,
            "km": round(km, 2) if km else None, "kcal": round(kcal) if kcal else None, "avg_hr": avg_hr, "max_hr": max_hr}


def parse(body: dict[str, Any]) -> dict[str, Any]:
    """{"metrics": {day: {metric: value}}, "sleep": {day: {start, end}},
    "workouts": {day: [...]}, "ignored": [names not used]}"""
    if looks_native(body):
        return {
            "metrics": {d: {k: float(v) for k, v in m.items() if isinstance(v, (int, float))} for d, m in body["metrics"].items() if _day(d)},
            "sleep": {d: t for d, t in (body.get("sleep") or {}).items() if _day(d) and isinstance(t, dict)},
            "workouts": {d: list(ws) for d, ws in (body.get("workouts") or {}).items() if _day(d) and isinstance(ws, list)},
            "ignored": [],
        }
    data = body.get("data") or {}
    readings: dict[tuple[str, str], list[float]] = defaultdict(list)
    sleep_times: dict[str, dict[str, str]] = {}
    ignored: set[str] = set()
    for m in data.get("metrics") or []:
        if not isinstance(m, dict):
            continue
        name = str(m.get("name") or "").strip().lower()
        units = str(m.get("units") or "")
        if name not in METRICS and name not in ("sleep_analysis", "heart_rate"):
            ignored.add(name)
            continue
        for r in m.get("data") or []:
            if not isinstance(r, dict) or not (day := _day(r.get("date"))):
                continue
            if name == "sleep_analysis":
                for field, metric in SLEEP_FIELDS.items():
                    v = _num(r.get(field))
                    if v is not None and not (field == "asleep" and r.get("totalSleep") is not None):
                        readings[(day, metric)].append(v / 60 if units.lower().startswith("min") else v)
                if r.get("sleepStart") or r.get("sleepEnd"):
                    sleep_times[day] = {"start": str(r.get("sleepStart") or "")[:25], "end": str(r.get("sleepEnd") or "")[:25]}
            elif name == "heart_rate":
                for field, metric in (("Avg", "heart_rate_avg"), ("Min", "heart_rate_min"), ("Max", "heart_rate_max")):
                    v = _num(r.get(field))
                    if v is not None:
                        readings[(day, metric)].append(v)
            else:
                metric = METRICS[name][0]
                v = _num(r.get("qty"))
                if v is not None:
                    readings[(day, metric)].append(convert(metric, units, v))
    workouts: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for w in data.get("workouts") or []:
        if not isinstance(w, dict) or not (day := _day(w.get("start"))):
            continue
        dist = w.get("distance")
        km = _num(dist)
        if km is not None and isinstance(dist, dict) and str(dist.get("units") or "").lower() in ("mi", "miles"):
            km *= 1.609344
        energy = w.get("activeEnergyBurned") or w.get("activeEnergy")
        kcal = _num(energy)
        if kcal is not None and isinstance(energy, dict) and str(energy.get("units") or "").lower() == "kj":
            kcal /= 4.184
        entry = workout_entry(w.get("name"), w.get("start"), _num(w.get("duration")), km, kcal,
                              _num(w.get("avgHeartRate") or w.get("heartRateAvg")), _num(w.get("maxHeartRate")))
        workouts[day].append(entry)
        if "run" in entry["name"].lower() and km:
            readings[(day, "running_km")].append(km)
    metrics: dict[str, dict[str, float]] = defaultdict(dict)
    for (day, metric), values in readings.items():
        metrics[day][metric] = combine(metric, values)
    return {"metrics": dict(metrics), "sleep": sleep_times, "workouts": dict(workouts), "ignored": sorted(ignored)}
