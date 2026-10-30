"""Everything the Health section shows, worked out in one place (SirisAI
3.51): readiness, sleep, activity and load, vitals, the weight trend, the
getting-sick warning, recent notes, and how the imports are doing. SirisOS
shows this (GET /siris/hub/v1/health/overview); chat and voice read it
through health_overview."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any

from app.core import clock
from app.health import scores

if TYPE_CHECKING:
    from app.core.siris import SirisAI

IMPORTS_KEY = "health_imports"


def today(siris: "SirisAI") -> date:
    return clock.now_local(siris.settings.timezone).date()


def days(siris: "SirisAI", on: date, n: int = 70) -> scores.Days:
    return siris.health.days((on - timedelta(days=n)).isoformat(), on.isoformat())


def note_import(siris: "SirisAI", source: str, result: dict[str, Any]) -> None:
    kept = dict(siris.state.get(IMPORTS_KEY) or {})
    kept[source] = {"at": datetime.now(UTC).isoformat(), **{k: result.get(k) for k in ("days", "readings", "workouts")}}
    siris.state.set(IMPORTS_KEY, kept)


def imports(siris: "SirisAI") -> dict[str, Any]:
    return dict(siris.state.get(IMPORTS_KEY) or {})


def build(siris: "SirisAI", on: date | None = None) -> dict[str, Any]:
    on = on or today(siris)
    data = days(siris, on)
    since = (on - timedelta(days=21)).isoformat()
    workouts = siris.health.workouts(since)
    today_m = data.get(on.isoformat()) or {}
    return {
        "today": on.isoformat(),
        "available": bool(data),
        "sources": {"days": len(data), "imports": imports(siris)},
        "readiness": scores.readiness(data, on),
        "sleep": scores.sleep(data, siris.health.sleep_times((on - timedelta(days=15)).isoformat()), on),
        "activity": {
            "steps": today_m.get("steps"), "active_energy_kcal": today_m.get("active_energy_kcal"),
            "exercise_minutes": today_m.get("exercise_minutes"), "stand_hours": today_m.get("stand_hours"),
            "load": scores.load(data, on),
            "exercise_series": scores.series(data, "exercise_minutes", on, 28),
            "steps_series": scores.series(data, "steps", on, 14),
            "workouts": workouts[:12],
        },
        "vitals": scores.vitals(data, on),
        "weight": scores.weight_trend(data, on, siris.settings.health_weight_goal_kg),
        "getting_sick": scores.getting_sick(data, on),
        "logged": {
            "water_ml": today_m.get("water_ml"), "caffeine_mg": today_m.get("caffeine_mg"),
            "alcohol_drinks": today_m.get("alcohol_drinks"), "mood": today_m.get("mood"),
            "notes": siris.health.notes((on - timedelta(days=7)).isoformat())[:20],
        },
    }


def text(overview: dict[str, Any]) -> str:
    """A spoken-length summary (voice, and a privacy-safe chat answer)."""
    parts = []
    r = overview.get("readiness")
    if r:
        parts.append(f"Readiness {r['score']}, {r['band'].lower()}: {r['advice']}")
    last = (overview.get("sleep") or {}).get("last_night")
    if last and last.get("hours"):
        score = overview["sleep"].get("score")
        parts.append(f"You slept {last['hours']:.1f} hours" + (f" (sleep score {score})" if score is not None else "") + ".")
    sick = overview.get("getting_sick")
    if sick:
        parts.append("Your body may be fighting something off: " + ", ".join(sick["today"]) + ".")
    w = overview.get("weight")
    if w and w.get("per_week") is not None:
        parts.append(f"Weight trend {w['trend']:.1f} kg ({w['per_week']:+.1f} kg a week).")
    load = (overview.get("activity") or {}).get("load") or {}
    if load.get("zone"):
        parts.append(f"Training load is in the {load['zone']} zone.")
    return " ".join(parts) or "No health data yet: connect Health Auto Export (see SirisOS → Training → Apple Health)."


def check_stale(siris: "SirisAI", now: datetime | None = None) -> dict[str, Any] | None:
    """'Health data has stopped arriving' when nothing new has come in for
    SIRISAI_HEALTH_STALE_HOURS (36). Health Auto Export fails quietly (iOS
    stops background refresh, the phone stays locked, a key changes); this
    says so. It closes itself when data arrives again."""
    hours = siris.settings.health_stale_hours
    last = siris.health.last_day()
    wanted = []
    if hours and last:
        age = ((now or datetime.now(UTC)) - datetime.fromisoformat(last)).total_seconds() / 3600
        if age > hours:
            wanted.append({
                "key": "health:stale",
                "kind": "alert",
                "title": f"Health data hasn't arrived for {age / 24:.1f} days" if age >= 48 else f"Health data hasn't arrived for {age:.0f} hours",
                "body": "The last import was " + last[:16].replace("T", " ") + " UTC. Open Health Auto Export on the phone (it only sends while unlocked), "
                        "check its automation is enabled, and see SirisOS → Training → Apple Health for what the phone sent lately.",
                "severity": "attention",
            })
    siris.attention.sync("health-import", wanted, outcome="Health data is arriving again")
    return wanted[0] if wanted else None
