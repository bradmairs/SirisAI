"""Health data pushed from a phone (Health Auto Export, an Apple Shortcuts
automation, an Apple Health backfill, or anything that can POST JSON) --
sleep, steps, heart rate, vitals, workouts -- so briefings, the Health
section and bedtime nudges know how you're actually doing.

Since 3.51 this is the one health store, for SirisOS too: every metric
Apple Health gives (see auto_export.py), the night's sleep times, each
workout, and notes logged by voice. Each source's reading is kept
(HealthReading) and the one to trust is written to HealthSample by
SIRISAI_HEALTH_SOURCE_PRIORITY, so a Shortcut and Health Auto Export
reporting the same day don't count twice. Workouts from two sources within
five minutes of each other are the same workout.

Per user (X-Siris-User / voice recognition), one value per metric per day.
The summary compares the latest day with your own 30-day baseline rather
than generic targets: "slept 5.9h, about 1.4h under your usual" means more
than "below 7h".
"""

import csv
import io
from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta
from statistics import mean
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import sessionmaker

from app.core.user_context import get_current_user_id
from app.db.models import HealthNote, HealthReading, HealthSample, HealthSleep, HealthWorkout
from app.health.auto_export import COMBINE, SLEEP_FIELDS, when

DEFAULT_PRIORITY = ("health_auto_export", "apple_export", "shortcut", "sirisos", "manual")

# Accepted metric names, with the aliases common exports use.
METRICS = {
    "sleep_hours": ("sleep_hours", "sleep", "sleep_duration_hours", "asleep_hours"),
    "steps": ("steps", "step_count"),
    "resting_heart_rate": ("resting_heart_rate", "resting_hr", "rhr"),
    "heart_rate_variability": ("heart_rate_variability", "hrv", "hrv_ms"),
    "active_energy_kcal": ("active_energy_kcal", "active_energy", "active_calories"),
    "weight_kg": ("weight_kg", "weight", "body_mass"),
    "exercise_minutes": ("exercise_minutes", "exercise_time", "workout_minutes"),
    "running_km": ("running_km", "run_km", "running_distance_km", "distance_running_km"),
}
# Every other metric Apple Health gives, under its own name only.
for _name in sorted(COMBINE):
    METRICS.setdefault(_name, (_name,))
METRICS.update({"water_ml": ("water_ml", "water"), "caffeine_mg": ("caffeine_mg", "caffeine"), "mood": ("mood",),
                "energy_level": ("energy_level",), "alcohol_drinks": ("alcohol_drinks", "alcohol", "drinks"),
                "meal_kcal_estimate": ("meal_kcal_estimate",), "meal_protein_g_estimate": ("meal_protein_g_estimate",)})
_ALIASES = {alias: name for name, aliases in METRICS.items() for alias in aliases}
_LABELS = {
    "sleep_hours": ("slept", "h"),
    "steps": ("walked", " steps"),
    "resting_heart_rate": ("resting heart rate", " bpm"),
    "heart_rate_variability": ("HRV", " ms"),
    "active_energy_kcal": ("active energy", " kJ"),  # kept in kcal, shown in kJ (SI, 4.05)
    "weight_kg": ("weight", " kg"),
    "exercise_minutes": ("exercise", " min"),
    "running_km": ("ran", " km"),
}
# How far from your own average counts as worth mentioning -- an absolute
# amount or a fraction of the average -- and which direction is the
# concerning one.
_NOTABLE = {
    "sleep_hours": ("absolute", 0.75, -1),
    "steps": ("fraction", 0.35, -1),
    "resting_heart_rate": ("absolute", 5, 1),
    "heart_rate_variability": ("fraction", 0.25, -1),
}


def normalize(metrics: dict[str, Any]) -> tuple[dict[str, float], list[str]]:
    clean: dict[str, float] = {}
    ignored: list[str] = []
    for key, value in metrics.items():
        name = _ALIASES.get(key.strip().lower().replace(" ", "_").replace("-", "_"))
        try:
            number = float(value)
        except (TypeError, ValueError):
            name = None
        if name is None:
            ignored.append(key)
            continue
        clean[name] = number
    return clean, ignored


NIGHT_ENDS_BY = 14  # a sleep that ends after 2pm has a nap folded in


def replaces_night(old: dict[str, str], new: dict[str, str]) -> bool:
    """Whether a newly exported night should replace the one on file. Health
    Auto Export re-sends the day all day long: a later export can fold an
    afternoon nap into "last night" (it ends after 2pm), or, sent "since the
    last sync", hold only the tail end of it (it starts later). Neither is a
    better view of the night, so the first full one stands. One that started
    as early and ended later (the first was sent mid-sleep) does replace it."""
    a0, a1, b0, b1 = when(old.get("start")), when(old.get("end")), when(new.get("start")), when(new.get("end"))
    if not (a0 and a1 and b0 and b1):
        return True
    if b1.hour >= NIGHT_ENDS_BY and a1.hour < NIGHT_ENDS_BY:
        return False
    return (b0 - a0).total_seconds() <= 30 * 60 and b1 >= a1


HOUR_SUMS_KEY = "health_hour_sums"


def merge_hour_sums(state: Any, parsed: dict[str, Any], user_id: str, keep_days: int = 10) -> dict[str, Any]:
    """Split syncs (4.17): Health Auto Export's Batch Requests send one sync as
    several requests, each with part of a day. Summed metrics (steps, energy,
    exercise minutes) are kept hour by hour, an hour sent again replacing
    itself, and the day's total is the sum of every hour received, so the
    parts add up and re-sends never double. Exports grouped by day (no hours)
    are left as they are."""
    sums = parsed.get("hour_sums") or {}
    if not sums:
        return parsed
    key = f"{HOUR_SUMS_KEY}:{user_id}"
    kept = dict(state.get(key) or {})
    metrics = {d: dict(m) for d, m in (parsed.get("metrics") or {}).items()}
    for day, by_metric in sums.items():
        known = dict(kept.get(day) or {})
        for metric, hours in by_metric.items():
            merged = {**(known.get(metric) or {}), **{h: round(v, 3) for h, v in hours.items()}}
            known[metric] = merged
            metrics.setdefault(day, {})[metric] = round(sum(merged.values()), 2)
        kept[day] = known
    for day in sorted(kept)[:-keep_days]:
        kept.pop(day)
    state.set(key, kept)
    return {**parsed, "metrics": metrics}


class HealthStore:
    def __init__(self, session_factory: sessionmaker, priority: Iterable[str] = DEFAULT_PRIORITY) -> None:
        self._session_factory = session_factory
        self.priority = list(priority)

    def _rank(self, source: str) -> int:
        return self.priority.index(source) if source in self.priority else len(self.priority)

    def record(self, metrics: dict[str, float], day: str, user_id: str | None = None, source: str = "shortcut") -> int:
        return self.record_days({day: metrics}, user_id, source)

    def record_days(self, days: dict[str, dict[str, float]], user_id: str | None = None, source: str = "shortcut") -> int:
        """Keep each value as this source's reading, then make the trusted
        value (HealthSample) the best-ranked source's. Returns readings kept."""
        user_id = user_id or get_current_user_id()
        if not days:
            return 0
        count = 0
        with self._session_factory() as session:
            wanted = sorted(days)
            for chunk in (wanted[i:i + 300] for i in range(0, len(wanted), 300)):
                readings = session.scalars(select(HealthReading).where(HealthReading.user_id == user_id, HealthReading.day.in_(chunk))).all()
                samples = session.scalars(select(HealthSample).where(HealthSample.user_id == user_id, HealthSample.day.in_(chunk))).all()
                by_key: dict[tuple[str, str], list[HealthReading]] = {}
                for r in readings:
                    by_key.setdefault((r.day, r.metric), []).append(r)
                sample_by_key = {(r.day, r.metric): r for r in samples}
                for day in chunk:
                    for metric, value in days[day].items():
                        rows = by_key.setdefault((day, metric), [])
                        mine = next((r for r in rows if r.source == source), None)
                        if mine is None:
                            mine = HealthReading(user_id=user_id, metric=metric, day=day, source=source, value=float(value))
                            session.add(mine)
                            rows.append(mine)
                        else:
                            mine.value, mine.updated_at = float(value), datetime.now(UTC)
                        count += 1
                        best = min(rows, key=lambda r: self._rank(r.source))
                        sample = sample_by_key.get((day, metric))
                        if sample is None:
                            sample = HealthSample(user_id=user_id, metric=metric, day=day, value=best.value)
                            session.add(sample)
                            sample_by_key[(day, metric)] = sample
                        else:
                            sample.value = best.value
            session.commit()
        return count

    def record_sleep(self, times: dict[str, dict[str, str]], user_id: str | None = None) -> None:
        user_id = user_id or get_current_user_id()
        with self._session_factory() as session:
            for day, t in times.items():
                row = session.scalars(select(HealthSleep).where(HealthSleep.user_id == user_id, HealthSleep.day == day)).first()
                row = row or HealthSleep(user_id=user_id, day=day)
                row.start, row.end = str(t.get("start") or "")[:25], str(t.get("end") or "")[:25]
                session.add(row)
            session.commit()

    def record_workouts(self, workouts: dict[str, list[dict[str, Any]]], user_id: str | None = None, source: str = "health_auto_export") -> int:
        """A day's workouts from this source replace its earlier ones; a
        workout another source already has (start within 5 minutes) is kept
        from whichever source ranks higher."""
        user_id = user_id or get_current_user_id()
        added = 0
        with self._session_factory() as session:
            for day, items in workouts.items():
                session.execute(delete(HealthWorkout).where(HealthWorkout.user_id == user_id, HealthWorkout.day == day, HealthWorkout.source == source))
                others = session.scalars(select(HealthWorkout).where(HealthWorkout.user_id == user_id, HealthWorkout.day == day)).all()
                for w in items:
                    start = _parse_time(w.get("start"))
                    twin = next((o for o in others if start and (t := _parse_time(o.start)) and abs((t - start).total_seconds()) <= 300), None)
                    if twin is not None:
                        if self._rank(twin.source) <= self._rank(source):
                            continue
                        session.delete(twin)
                    session.add(HealthWorkout(user_id=user_id, day=day, source=source, start=str(w.get("start") or "")[:25],
                                              name=str(w.get("name") or "Workout")[:60], minutes=w.get("minutes"), km=w.get("km"),
                                              kcal=w.get("kcal"), avg_hr=w.get("avg_hr"), max_hr=w.get("max_hr")))
                    added += 1
            session.commit()
        return added

    def store_parsed(self, parsed: dict[str, Any], source: str, user_id: str | None = None) -> dict[str, int]:
        """Everything one import brought (auto_export.parse / apple_export).
        A night already recorded isn't replaced by a smaller view of it (3.62):
        see replaces_night."""
        metrics = {d: dict(m) for d, m in (parsed.get("metrics") or {}).items()}
        sleep = dict(parsed.get("sleep") or {})
        known = self.sleep_times(min(sleep), user_id) if sleep else {}
        for day, new in list(sleep.items()):
            if day in known and not replaces_night(known[day], new):
                sleep.pop(day)
                for metric in set(SLEEP_FIELDS.values()):
                    (metrics.get(day) or {}).pop(metric, None)
        readings = self.record_days(metrics, user_id, source)
        self.record_sleep(sleep, user_id)
        workouts = self.record_workouts(parsed.get("workouts") or {}, user_id, source)
        return {"days": len(parsed.get("metrics") or {}), "readings": readings, "workouts": workouts}

    def sources(self, day: str, user_id: str | None = None) -> dict[str, Any]:
        """Each metric on a day with every source's value and the one trusted (4.17), for SirisOS's data inspector."""
        user_id = user_id or get_current_user_id()
        with self._session_factory() as session:
            readings = session.scalars(select(HealthReading).where(HealthReading.user_id == user_id, HealthReading.day == day)).all()
            out: dict[str, dict[str, Any]] = {}
            for r in readings:
                entry = out.setdefault(r.metric, {"metric": r.metric, "sources": []})
                entry["sources"].append({"source": r.source, "value": r.value, "updated_at": r.updated_at.replace(tzinfo=UTC).isoformat() if r.updated_at else None})
            for entry in out.values():
                entry["sources"].sort(key=lambda s: self._rank(s["source"]))
                entry["trusted"] = entry["sources"][0]["source"]
                entry["value"] = entry["sources"][0]["value"]
            workouts = session.scalars(select(HealthWorkout).where(HealthWorkout.user_id == user_id, HealthWorkout.day == day)).all()
            sleep = session.scalars(select(HealthSleep).where(HealthSleep.user_id == user_id, HealthSleep.day == day)).first()
            return {"day": day, "priority": self.priority, "metrics": sorted(out.values(), key=lambda e: e["metric"]),
                    "workouts": [{"name": w.name, "start": w.start, "minutes": w.minutes, "source": w.source} for w in workouts],
                    "sleep": {"start": sleep.start, "end": sleep.end} if sleep else None}

    def series(self, metric: str, since: str, user_id: str | None = None) -> list[tuple[str, float]]:
        user_id = user_id or get_current_user_id()
        with self._session_factory() as session:
            rows = session.scalars(
                select(HealthSample)
                .where(HealthSample.user_id == user_id, HealthSample.metric == metric, HealthSample.day >= since)
                .order_by(HealthSample.day)
            ).all()
            return [(r.day, r.value) for r in rows]

    def days(self, since: str, until: str | None = None, user_id: str | None = None) -> dict[str, dict[str, float]]:
        """Day -> every trusted metric that day."""
        user_id = user_id or get_current_user_id()
        out: dict[str, dict[str, float]] = {}
        with self._session_factory() as session:
            stmt = select(HealthSample).where(HealthSample.user_id == user_id, HealthSample.day >= since)
            if until:
                stmt = stmt.where(HealthSample.day <= until)
            for r in session.scalars(stmt):
                out.setdefault(r.day, {})[r.metric] = r.value
        return out

    def sleep_times(self, since: str, user_id: str | None = None) -> dict[str, dict[str, str]]:
        user_id = user_id or get_current_user_id()
        with self._session_factory() as session:
            rows = session.scalars(select(HealthSleep).where(HealthSleep.user_id == user_id, HealthSleep.day >= since)).all()
            return {r.day: {"start": r.start, "end": r.end} for r in rows}

    def workouts(self, since: str, user_id: str | None = None) -> list[dict[str, Any]]:
        """Newest first."""
        user_id = user_id or get_current_user_id()
        with self._session_factory() as session:
            rows = session.scalars(
                select(HealthWorkout).where(HealthWorkout.user_id == user_id, HealthWorkout.day >= since).order_by(HealthWorkout.start.desc())
            ).all()
            return [{"date": r.day, "name": r.name, "start": r.start, "minutes": r.minutes, "km": r.km, "kcal": r.kcal,
                     "avg_hr": r.avg_hr, "max_hr": r.max_hr, "source": r.source} for r in rows]

    def add_note(self, kind: str, text: str, day: str, user_id: str | None = None) -> dict[str, Any]:
        user_id = user_id or get_current_user_id()
        with self._session_factory() as session:
            row = HealthNote(user_id=user_id, day=day, kind=kind, text=text[:500])
            session.add(row)
            session.commit()
            return {"id": row.id, "day": day, "kind": kind, "text": row.text}

    def notes(self, since: str, user_id: str | None = None) -> list[dict[str, Any]]:
        user_id = user_id or get_current_user_id()
        with self._session_factory() as session:
            rows = session.scalars(select(HealthNote).where(HealthNote.user_id == user_id, HealthNote.day >= since).order_by(HealthNote.at.desc())).all()
            return [{"id": r.id, "day": r.day, "kind": r.kind, "text": r.text, "at": r.at.isoformat()} for r in rows]

    def last_day(self, user_id: str | None = None) -> str | None:
        user_id = user_id or get_current_user_id()
        with self._session_factory() as session:
            row = session.scalars(select(HealthReading).where(HealthReading.user_id == user_id).order_by(HealthReading.updated_at.desc())).first()
            return row.updated_at.replace(tzinfo=UTC).isoformat() if row else None

    def delete(self, start: str, end: str, source: str | None = None, user_id: str | None = None) -> dict[str, int]:
        """Remove a date range (inclusive), all of it or one source's. The
        trusted values are rebuilt from whatever is left."""
        user_id = user_id or get_current_user_id()
        with self._session_factory() as session:
            in_range = (HealthReading.user_id == user_id, HealthReading.day >= start, HealthReading.day <= end)
            stmt = delete(HealthReading).where(*in_range, *([HealthReading.source == source] if source else []))
            readings = session.execute(stmt).rowcount or 0
            session.execute(delete(HealthSample).where(HealthSample.user_id == user_id, HealthSample.day >= start, HealthSample.day <= end))
            ws = delete(HealthWorkout).where(HealthWorkout.user_id == user_id, HealthWorkout.day >= start, HealthWorkout.day <= end, *([HealthWorkout.source == source] if source else []))
            workouts = session.execute(ws).rowcount or 0
            if not source:
                session.execute(delete(HealthSleep).where(HealthSleep.user_id == user_id, HealthSleep.day >= start, HealthSleep.day <= end))
                session.execute(delete(HealthNote).where(HealthNote.user_id == user_id, HealthNote.day >= start, HealthNote.day <= end))
            left = [(r.day, r.metric, r.source, r.value) for r in session.scalars(select(HealthReading).where(*in_range))]
            session.commit()
        best: dict[tuple[str, str], tuple[str, float]] = {}
        for day, metric, src, value in left:
            if (day, metric) not in best or self._rank(src) < self._rank(best[(day, metric)][0]):
                best[(day, metric)] = (src, value)
        rebuilt: dict[str, dict[str, float]] = {}
        for (day, metric), (_, value) in best.items():
            rebuilt.setdefault(day, {})[metric] = value
        with self._session_factory() as session:
            for day, metrics in rebuilt.items():
                for metric, value in metrics.items():
                    session.add(HealthSample(user_id=user_id, metric=metric, day=day, value=value))
            session.commit()
        return {"readings": readings, "workouts": workouts}

    def export_csv(self, start: str, end: str, user_id: str | None = None) -> str:
        """date, metric, value (the trusted values), then the workouts."""
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(["date", "metric", "value"])
        for day, metrics in sorted(self.days(start, end, user_id).items()):
            for metric, value in sorted(metrics.items()):
                writer.writerow([day, metric, value])
        writer.writerow([])
        writer.writerow(["workout_date", "name", "start", "minutes", "km", "kcal", "avg_hr", "max_hr"])
        for w in reversed(self.workouts(start, user_id)):
            if w["date"] <= end:
                writer.writerow([w["date"], w["name"], w["start"], w["minutes"], w["km"], w["kcal"], w["avg_hr"], w["max_hr"]])
        return out.getvalue()


def _parse_time(text: Any) -> datetime | None:
    raw = str(text or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S %z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(raw[:25], fmt).replace(tzinfo=None)
        except ValueError:
            continue
    return None


def kj(kcal: float) -> float:
    """Energy is stored in kcal, as Apple Health sends it, and shown in kJ (SI)."""
    return kcal * 4.184


def _fmt(metric: str, value: float) -> str:
    label, unit = _LABELS[metric]
    value = kj(value) if metric == "active_energy_kcal" else value
    shown = f"{value:,.0f}" if metric in ("steps", "active_energy_kcal") else f"{value:g}" if metric != "sleep_hours" else f"{value:.1f}"
    return f"{label} {shown}{unit}"


def summary(store: HealthStore, today: date, user_id: str | None = None) -> dict[str, Any]:
    since = (today - timedelta(days=30)).isoformat()
    metrics: dict[str, Any] = {}
    notes: list[str] = []
    for metric in METRICS:
        series = store.series(metric, since, user_id)
        if not series:
            continue
        latest_day, latest = series[-1]
        history = [v for d, v in series[:-1]]
        baseline = mean(history) if len(history) >= 3 else None
        week = [v for d, v in series if d >= (today - timedelta(days=7)).isoformat()]
        entry: dict[str, Any] = {"latest": latest, "day": latest_day, "week_average": round(mean(week), 2) if week else None, "baseline": round(baseline, 2) if baseline is not None else None}
        metrics[metric] = entry
        if baseline is None or metric not in _NOTABLE:
            continue
        kind, threshold, direction = _NOTABLE[metric]
        delta = latest - baseline
        limit = threshold * baseline if kind == "fraction" else threshold
        if abs(delta) >= limit and delta * direction > 0:
            unit = _LABELS[metric][1].strip() or ""
            amount = f"{abs(delta):,.0f}" if metric == "steps" else f"{abs(delta):.1f}"
            notes.append(f"{_fmt(metric, latest)} -- {amount}{' ' + unit if unit and metric != 'sleep_hours' else unit} {'under' if delta < 0 else 'over'} your usual")
    return {"metrics": metrics, "notes": notes}
