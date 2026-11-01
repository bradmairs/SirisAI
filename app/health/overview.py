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
from app.health import battery, scores, strain

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
        return {**live, "locked": False, "locked_at": None} if live else None
    kept[on.isoformat()] = {**live, "locked": True, "locked_at": now.isoformat(timespec="minutes")}
    for day in sorted(kept)[:-120]:
        kept.pop(day)
    siris.state.set(key, kept)
    return kept[on.isoformat()]


def today(siris: "SirisAI") -> date:
    return clock.now_local(siris.settings.timezone).date()


def days(siris: "SirisAI", on: date, n: int = 70) -> scores.Days:
    return siris.health.days((on - timedelta(days=n)).isoformat(), on.isoformat())


def note_import(siris: "SirisAI", source: str, result: dict[str, Any], meta: dict[str, Any] | None = None) -> None:
    """Keep when each source last sent, and (Health Auto Export, 4.03) what the
    export looked like, metrics and workouts separately, for setup_check."""
    kept = dict(siris.state.get(IMPORTS_KEY) or {})
    entry = {"at": datetime.now(UTC).isoformat(), "sent_on": clock.now_local(siris.settings.timezone).date().isoformat(),
             **{k: result.get(k) for k in ("days", "readings", "workouts")}, **(meta or {})}
    key = f"{source}:workouts" if meta and meta.get("kind") == "workouts" else source
    kept[key] = entry
    siris.state.set(IMPORTS_KEY, kept)


def imports(siris: "SirisAI") -> dict[str, Any]:
    return dict(siris.state.get(IMPORTS_KEY) or {})


def _factors(siris: "SirisAI") -> list[dict[str, Any]]:
    from app.health import insights

    try:
        return list(insights.current(siris) or [])
    except Exception:  # noqa: BLE001 - the report stands without them
        return []


def setup_check(siris: "SirisAI", now: datetime | None = None) -> dict[str, Any]:
    """Is Health Auto Export set up the way SirisAI needs (4.03)? From what the
    last imports looked like: grouped by hour, sending whole days (the
    previous 7), and a workouts automation too. Each problem says which
    setting to change. 4.04: the date range to use is **Default** (all of
    yesterday plus today so far). "Previous 7 Days" means the seven *full*
    days before today, so today never arrived: no sleep, steps, strain or
    energy for today. An export sent today that stops at yesterday is
    flagged."""
    kept = imports(siris)
    hae = {k: v for k, v in kept.items() if k.startswith("health_auto_export")}
    metrics, workouts = hae.get("health_auto_export"), hae.get("health_auto_export:workouts")
    problems = []
    if not hae:
        return {"connected": False, "problems": ["Nothing from Health Auto Export yet: set up the automation (SirisOS → Training → Apple Health)."]}
    if metrics and metrics.get("aggregation") == "day":
        problems.append("Health metrics arrive grouped by day: set Aggregate (time grouping) to Hours, so readiness can use your overnight HRV and energy can follow the day.")
    if metrics and metrics.get("last_day") and metrics.get("sent_on") and metrics["last_day"] < metrics["sent_on"]:
        problems.append("The last health export stopped at yesterday, so today's sleep, steps, strain and energy are missing: "
                        "set Date Range to Default (all of yesterday plus today so far). \"Previous 7 Days\" leaves today out.")
    elif metrics and metrics.get("days_covered") == 1 and (metrics.get("first_hour") or 0) >= 3:
        problems.append("The last health export only held part of today: set Date Range to Default (all of yesterday plus today so far), so each sync sends whole days.")
    if not workouts:
        problems.append("No workouts have arrived: add a second automation for Workouts (Date Range Default, hourly), or running km and strain won't count.")
    return {"connected": True, "metrics": metrics, "workouts": workouts, "problems": problems}


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
        "strain": strain.build(siris, ready, on),
        "sleep": scores.sleep(data, siris.health.sleep_times((on - timedelta(days=15)).isoformat()), on),
        "sleep_week": scores.sleep_week(data, siris.health.sleep_times((on - timedelta(days=8)).isoformat()), on, _factors(siris)),
        "setup": setup_check(siris, now),
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
    st = overview.get("strain")
    if st:
        parts.append(f"Strain {st['today']} today, {st['status']} for a {st['target'][0]}-{st['target'][1]} target.")
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
