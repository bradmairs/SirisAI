"""Health data pushed from a phone (an Apple Shortcuts automation reading
Apple Health, or anything that can POST JSON) -- sleep, steps, heart rate --
so briefings and bedtime nudges know how you're actually doing.

Per user (X-Siris-User / voice recognition), one value per metric per day.
The summary compares the latest day with your own 30-day baseline rather
than generic targets: "slept 5.9h, about 1.4h under your usual" means more
than "below 7h".
"""

from datetime import date, timedelta
from statistics import mean
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.core.user_context import get_current_user_id
from app.db.models import HealthSample

# Accepted metric names, with the aliases common exports use.
METRICS = {
    "sleep_hours": ("sleep_hours", "sleep", "sleep_duration_hours", "asleep_hours"),
    "steps": ("steps", "step_count"),
    "resting_heart_rate": ("resting_heart_rate", "resting_hr", "rhr"),
    "heart_rate_variability": ("heart_rate_variability", "hrv", "hrv_ms"),
    "active_energy_kcal": ("active_energy_kcal", "active_energy", "active_calories"),
    "weight_kg": ("weight_kg", "weight", "body_mass"),
    "exercise_minutes": ("exercise_minutes", "exercise_time", "workout_minutes"),
}
_ALIASES = {alias: name for name, aliases in METRICS.items() for alias in aliases}
_LABELS = {
    "sleep_hours": ("slept", "h"),
    "steps": ("walked", " steps"),
    "resting_heart_rate": ("resting heart rate", " bpm"),
    "heart_rate_variability": ("HRV", " ms"),
    "active_energy_kcal": ("active energy", " kcal"),
    "weight_kg": ("weight", " kg"),
    "exercise_minutes": ("exercise", " min"),
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


class HealthStore:
    def __init__(self, session_factory: sessionmaker) -> None:
        self._session_factory = session_factory

    def record(self, metrics: dict[str, float], day: str, user_id: str | None = None) -> int:
        user_id = user_id or get_current_user_id()
        with self._session_factory() as session:
            for metric, value in metrics.items():
                row = session.scalars(
                    select(HealthSample).where(HealthSample.user_id == user_id, HealthSample.metric == metric, HealthSample.day == day)
                ).first()
                if row is None:
                    session.add(HealthSample(user_id=user_id, metric=metric, day=day, value=value))
                else:
                    row.value = value
            session.commit()
        return len(metrics)

    def series(self, metric: str, since: str, user_id: str | None = None) -> list[tuple[str, float]]:
        user_id = user_id or get_current_user_id()
        with self._session_factory() as session:
            rows = session.scalars(
                select(HealthSample)
                .where(HealthSample.user_id == user_id, HealthSample.metric == metric, HealthSample.day >= since)
                .order_by(HealthSample.day)
            ).all()
            return [(r.day, r.value) for r in rows]


def _fmt(metric: str, value: float) -> str:
    label, unit = _LABELS[metric]
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
