"""Everything the Health section shows, worked out in one place (SirisAI
3.51): readiness (kept as it was each morning, 3.62), energy (3.62), sleep, activity and load, vitals, the weight trend, the
getting-sick warning, recent notes, and how the imports are doing. SirisOS
shows this (GET /siris/hub/v1/health/overview); chat and voice read it
through health_overview."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any

from app.core import clock
from app.core.user_context import get_current_user_id
from app.health import battery, scores

if TYPE_CHECKING:
    from app.core.siris import SirisAI

IMPORTS_KEY = "health_imports"
LATEST_LOCK_HOUR = 14


def morning_readiness(siris: "SirisAI", on: date, data: scores.Days, now: datetime | None = None) -> dict[str, Any] | None:
    """Readiness, kept once the morning is over (3.62). It's worked out live
    until the night is in (today's sleep has arrived) and it's past
    SIRISAI_HEALTH_READINESS_LOCK_HOUR, or until 2pm whatever's arrived;
    from then on the kept one is returned, so daytime HRV, a workout or a
    nap don't move it. Past days are kept the first time they're asked for."""
    now = now or clock.now_local(siris.settings.timezone).replace(tzinfo=None)
    key = f"health_readiness:{get_current_user_id()}"
    kept = dict(siris.state.get(key) or {})
    if on.isoformat() in kept:
        return kept[on.isoformat()]
    live = scores.readiness(data, on)
    night_in = (data.get(on.isoformat()) or {}).get("sleep_hours") is not None
    hour = now.hour if now.date() == on else 24 if now.date() > on else -1
    if live is None or not (hour >= LATEST_LOCK_HOUR or (night_in and hour >= siris.settings.health_readiness_lock_hour)):
        return {**live, "locked": False} if live else None
    kept[on.isoformat()] = {**live, "locked": True, "locked_at": now.isoformat(timespec="minutes")}
    for day in sorted(kept)[:-120]:
        kept.pop(day)
    siris.state.set(key, kept)
    return kept[on.isoformat()]


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


def build(siris: "SirisAI", on: date | None = None, now: datetime | None = None) -> dict[str, Any]:
    now = now or clock.now_local(siris.settings.timezone).replace(tzinfo=None)
    on = on or now.date()
    data = days(siris, on)
    ready = morning_readiness(siris, on, data, now)
    since = (on - timedelta(days=21)).isoformat()
    workouts = siris.health.workouts(since)
    today_m = data.get(on.isoformat()) or {}
    return {
        "today": on.isoformat(),
        "available": bool(data),
        "sources": {"days": len(data), "imports": imports(siris)},
        "readiness": ready,
        "energy": battery.today(siris, ready, data, on, now) if on == now.date() else None,
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
        "trends": scores.trends(data, on),
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
    e = overview.get("energy")
    if e:
        parts.append(f"Energy is at {e['now']} (it started at {e['start']}). {e['summary']}")
    last = (overview.get("sleep") or {}).get("last_night")
    if last and last.get("hours"):
        score = overview["sleep"].get("score")
        parts.append(f"You slept {last['hours']:.1f} hours" + (f" (sleep score {score})" if score is not None else "") + ".")
    sick = overview.get("getting_sick")
    if sick:
        lead = "Early signs your body may be fighting something off" if sick.get("level") == "early" else "Your body may be fighting something off"
        parts.append(f"{lead}: " + ", ".join(sick["today"]) + ".")
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
