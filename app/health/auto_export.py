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
converted to kg, km, kcal, °C and percent.

**Overnight HRV** (3.62): when readings carry their time (aggregated by hour,
or not at all), the HRV taken while asleep (inside the night's sleep, else
before 7am) is also kept as `hrv_overnight`, for the wake-up day. The whole
day's HRV falls with every workout and stressful hour, so readiness uses the
overnight figure. **Hourly** buckets of heart rate, active energy, steps,
HRV and mindful minutes come back as `intraday` for the energy score
(app/health/battery.py). A day-aggregated export (every reading at
midnight) has neither. SirisOS used to keep its own
copies of this reader; since 5.8 it passes the raw export here instead.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
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
COMBINE.update({"heart_rate_min": "min", "heart_rate_max": "max", "heart_rate_avg": "mean", "running_km": "sum", "hrv_overnight": "mean"})


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


def when(value: Any) -> datetime | None:
    """The reading's local wall-clock time ("2026-10-28 03:00:00 +1100")."""
    text = str(value or "").strip().replace("T", " ")
    try:
        return datetime.strptime(text[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        try:
            return datetime.strptime(text[:16], "%Y-%m-%d %H:%M")
        except ValueError:
            return None


def night_of(t: datetime, windows: dict[str, tuple[datetime, datetime]]) -> str | None:
    """The wake-up day whose sleep this moment falls in, else (no sleep
    recorded around it) its own day if it's before 7am."""
    for day, (start, end) in windows.items():
        if start <= t <= end:
            return day
    near = any(abs((t - end).total_seconds()) < 18 * 3600 for _, end in windows.values())
    return t.date().isoformat() if not near and t.hour < 7 else None


INTRADAY = {"heart_rate": "hr", "active_energy": "kcal", "step_count": "steps", "heart_rate_variability": "hrv",
            "heart_rate_variability_sdnn": "hrv", "mindful_minutes": "mindful"}


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
            "intraday": {},
            "meta": None,
        }
    data = body.get("data") or {}
    readings: dict[tuple[str, str], list[float]] = defaultdict(list)
    sleep_times: dict[str, dict[str, str]] = {}
    ignored: set[str] = set()
    windows: dict[str, tuple[datetime, datetime]] = {}
    for m in data.get("metrics") or []:
        if isinstance(m, dict) and str(m.get("name") or "").strip().lower() == "sleep_analysis":
            for day, night in nights(m.get("data") or [], str(m.get("units") or "")).items():
                for metric, v in night["hours"].items():
                    readings[(day, metric)].append(v)
                if night.get("start") is not None:
                    windows[day] = (night["start"] - timedelta(minutes=15), night["end"])
                    sleep_times[day] = {"start": night["raw_start"], "end": night["raw_end"]}
    hourly: dict[str, dict[str, dict[str, list[float]]]] = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    # 4.17: every summed metric hour by hour, so a sync split into several requests adds up instead of replacing the day.
    sums: dict[str, dict[str, dict[str, float]]] = defaultdict(lambda: defaultdict(lambda: defaultdict(float)))
    hr_samples: list[list[Any]] = []
    for m in data.get("metrics") or []:
        if not isinstance(m, dict):
            continue
        name = str(m.get("name") or "").strip().lower()
        units = str(m.get("units") or "")
        if name not in METRICS and name not in ("sleep_analysis", "heart_rate"):
            ignored.add(name)
            continue
        by_day = _midnight_only(m)
        for r in m.get("data") or []:
            if not isinstance(r, dict) or not (day := _day(r.get("date"))):
                continue
            if name == "sleep_analysis":
                continue  # read whole nights above (nights())
            elif name == "heart_rate":
                for field, metric in (("Avg", "heart_rate_avg"), ("Min", "heart_rate_min"), ("Max", "heart_rate_max")):
                    v = _num(r.get(field) if r.get(field) is not None else r.get("qty"))  # qty: each reading, not aggregated
                    if v is not None:
                        readings[(day, metric)].append(v)
            else:
                metric = METRICS[name][0]
                v = _num(r.get("qty"))
                if v is not None:
                    readings[(day, metric)].append(convert(metric, units, v))
            t = when(r.get("date"))
            if t is None or by_day:
                continue  # aggregated by day: no time of day to go on
            if name in METRICS and METRICS[name][1] == "sum" and (v := _num(r.get("qty"))) is not None:
                metric = METRICS[name][0]
                sums[day][metric][f"{t.hour:02d}"] += convert(metric, units, v)
            if name == "heart_rate" and (pt := _points([r])):
                hr_samples.append(pt[0])  # 4.39: each reading, for lining sets up with heart rate
            if name in INTRADAY:
                v = _num(r.get("Avg") if name == "heart_rate" and r.get("Avg") is not None else r.get("qty"))
                if v is not None:
                    v = convert("active_energy_kcal", units, v) if name == "active_energy" else v
                    hourly[day][f"{t.hour:02d}"][INTRADAY[name]].append(v)
            if name in ("heart_rate_variability", "heart_rate_variability_sdnn") and (v := _num(r.get("qty"))) is not None:
                if (night := night_of(t, windows)):
                    readings[(night, "hrv_overnight")].append(v)
    workouts: dict[str, list[dict[str, Any]]] = defaultdict(list)
    series: list[dict[str, Any]] = []
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
        if (s := workout_series(w)) is not None:
            series.append(s)
        if "run" in entry["name"].lower() and km:
            readings[(day, "running_km")].append(km)
    metrics: dict[str, dict[str, float]] = defaultdict(dict)
    for (day, metric), values in readings.items():
        metrics[day][metric] = combine(metric, values)
    intraday = {day: {hour: {k: round(sum(v) if k in ("kcal", "steps", "mindful") else mean(v), 1) for k, v in vals.items()}
                      for hour, vals in hours.items()} for day, hours in hourly.items()}
    return {"metrics": dict(metrics), "sleep": sleep_times, "workouts": dict(workouts), "ignored": sorted(ignored), "intraday": intraday,
            "hour_sums": {d: {m: dict(h) for m, h in ms.items()} for d, ms in sums.items()}, "workout_series": series, "hr_samples": hr_samples,
            "meta": describe(data)}


def _points(items: Any) -> list[list[Any]]:
    out = []
    for p in items if isinstance(items, list) else []:
        if not isinstance(p, dict):
            continue
        t = str(p.get("date") or p.get("start") or "")[:25]
        avg = _num(p.get("Avg") if p.get("Avg") is not None else p.get("avg") if p.get("avg") is not None else p.get("qty"))
        top = _num(p.get("Max") if p.get("Max") is not None else p.get("max"))
        if t and avg is not None:
            out.append([t, round(avg, 1), round(top if top is not None else avg, 1)])
    return out


def workout_series(w: dict[str, Any]) -> dict[str, Any] | None:
    """A workout's heart rate over time (4.28), from Health Auto Export's
    workout heart-rate data (one point a minute, or each sample, with Avg/Max
    or qty), and its active energy over time when sent. None when the export
    has neither (the Workouts automation needs heart-rate data turned on)."""
    hr: list[list[Any]] = []
    for key in ("heartRateData", "heartRate", "heart_rate", "heartRateSamples"):
        if isinstance(w.get(key), list):
            hr = _points(w[key])
            if hr:
                break
    kcal = []
    energy = w.get("activeEnergy")
    if isinstance(energy, list):
        kcal = [[p[0], p[1]] for p in _points(energy)]
    if not hr:
        return None
    return {"name": str(w.get("name") or "Workout")[:60], "start": str(w.get("start") or "")[:25], "end": str(w.get("end") or "")[:25],
            "hr": sorted(hr), "kcal": sorted(kcal)}


SEGMENT_STAGES = {"core": "core", "deep": "deep", "rem": "rem", "awake": "awake", "inbed": "inBed", "asleep": "asleep", "unspecified": "asleep",
                  "asleepcore": "core", "asleepdeep": "deep", "asleeprem": "rem", "asleepunspecified": "asleep"}
NIGHT_GAP = timedelta(hours=2)


def nights(records: list[Any], units: str = "hr") -> dict[str, dict[str, Any]]:
    """Health Auto Export's sleep, whichever way it's sent (4.04), as one
    night per wake-up day: {day: {hours: {metric: h}, start, end, raw_start,
    raw_end}}.
    - Summarised a night at a time (one record with sleepStart/sleepEnd).
    - Summarised but grouped by hour: several records for one night, some
      dated before midnight. They're joined back into the night, which
      belongs to the morning you woke up (before 4.04 the part before
      midnight went to the previous day and the night looked short).
    - Not summarised: a segment per stage (startDate, endDate, value "Core"/
      "Deep"/"REM"/"Awake"/"In Bed"). Before 4.04 these were dropped, so
      sleep showed nothing.
    Pieces less than two hours apart are one sleep; when a day has more than
    one (a nap), the longest is the night."""
    per_hour = 1 / 60 if units.lower().startswith("min") else 1.0
    pieces: list[tuple[datetime, datetime, str, str, dict[str, float]]] = []
    untimed: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for r in records:
        if not isinstance(r, dict):
            continue
        stage = str(r.get("value") or "").lower().replace(" ", "").replace("_", "")
        if r.get("startDate") and r.get("endDate") and stage:
            a, b = when(r.get("startDate")), when(r.get("endDate"))
            field = SEGMENT_STAGES.get(stage)
            if a and b and b > a and field:
                pieces.append((a, b, str(r["startDate"])[:25], str(r["endDate"])[:25], {field: (b - a).total_seconds() / 3600, "_seg": 1.0}))
            continue
        hours: dict[str, float] = {}
        for field in SLEEP_FIELDS:
            v = _num(r.get(field))
            if v is not None and not (field == "asleep" and r.get("totalSleep") is not None):
                hours[field] = v * per_hour
        raw_a = r.get("sleepStart") or r.get("inBedStart")
        raw_b = r.get("sleepEnd") or r.get("inBedEnd")
        a, b = when(raw_a), when(raw_b)
        if a and b and b >= a:
            pieces.append((a, b, str(raw_a)[:25], str(raw_b)[:25], hours))
        elif (day := _day(r.get("date"))):
            for field, v in hours.items():
                untimed[day][field] += v
    sessions: list[dict[str, Any]] = []
    for a, b, ra, rb, hours in sorted(pieces, key=lambda p: p[0]):
        cur = sessions[-1] if sessions else None
        if cur and a <= cur["end"] + NIGHT_GAP:
            if b > cur["end"]:
                cur["end"], cur["raw_end"] = b, rb
            for field, v in hours.items():
                cur["fields"][field] = cur["fields"].get(field, 0.0) + v
        else:
            sessions.append({"start": a, "end": b, "raw_start": ra, "raw_end": rb, "fields": dict(hours)})
            cur = sessions[-1]
        if cur["fields"].pop("_seg", None):
            cur["segments"] = True
    out: dict[str, dict[str, Any]] = {}
    for sess in sessions:
        day = sess["end"].date().isoformat()
        if day in out and (out[day]["end"] - out[day]["start"]) >= (sess["end"] - sess["start"]):
            continue  # a nap: the night stands
        out[day] = sess
    result: dict[str, dict[str, Any]] = {}
    for day, sess in out.items():
        f = dict(sess["fields"])
        if sess.get("segments"):  # stages add up to the sleep; "asleep" alone is the unstaged part
            f["asleep"] = f.get("asleep", 0.0) + sum(f.get(k, 0.0) for k in ("core", "deep", "rem"))
            if not f["asleep"]:
                f.pop("asleep")
        result[day] = {"hours": {SLEEP_FIELDS[k]: round(v, 2) for k, v in f.items()}, "start": sess["start"], "end": sess["end"],
                       "raw_start": sess["raw_start"], "raw_end": sess["raw_end"]}
    for day, f in untimed.items():
        if day not in result:
            result[day] = {"hours": {SLEEP_FIELDS[k]: round(v, 2) for k, v in f.items()}}
    return result


def describe(data: dict[str, Any]) -> dict[str, Any]:
    """What an export looked like (4.03), for the setup check: metrics or
    workouts, grouped by day, hour or not at all, how many days it covered,
    and the first hour it held of its last day (a 'since last sync' export
    starts late in the day)."""
    times = [t for m in data.get("metrics") or [] if isinstance(m, dict) and str(m.get("name") or "").lower() != "sleep_analysis"
             for r in m.get("data") or [] if isinstance(r, dict) and (t := when(r.get("date")))]
    workout_days = {d for w in data.get("workouts") or [] if isinstance(w, dict) and (d := _day(w.get("start")))}
    if not times:
        return {"kind": "workouts", "days_covered": len(workout_days), "workouts_sent": len(data.get("workouts") or [])}
    if all((t.hour, t.minute, t.second) == (0, 0, 0) for t in times):
        aggregation = "day"
    elif all((t.minute, t.second) == (0, 0) for t in times):
        aggregation = "hour"
    else:
        aggregation = "none"
    last = max(t.date() for t in times)
    return {"kind": "metrics", "aggregation": aggregation, "days_covered": len({t.date() for t in times}), "last_day": last.isoformat(),
            "first_hour": min(t.hour for t in times if t.date() == last), "workouts_sent": len(data.get("workouts") or []),
            "workout_days": len(workout_days)}


def _midnight_only(metric: dict[str, Any]) -> bool:
    """Every reading at midnight: the export is aggregated by day."""
    times = [str(r.get("date") or "")[11:19] for r in metric.get("data") or [] if isinstance(r, dict)]
    return bool(times) and all(t in ("00:00:00", "") for t in times)
