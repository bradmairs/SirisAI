"""Health insights (SirisAI 3.51): the getting-sick warning, what your sleep
goes with, tonight's bedtime, and goals tied to a health number.

- **Getting sick** (daily at SIRISAI_HEALTH_CHECK_HOUR_LOCAL, 10am, after the
  morning's data is in): temperature, resting heart rate and HRV pointing
  the same way two days running (scores.getting_sick) raises "Your body may
  be fighting something off" in the feed and on the phone, listing this
  evening's plans, with **Block my evening** (a "Rest" event, 5–10pm, if a
  writable calendar is set up). SirisOS's training plan rests that day.
- **What your sleep goes with** (weekly, and `health_insights`): nights with
  and without each of these are compared, with at least 4 nights each way
  and a 20-minute difference to count:
  - a calendar event ending after 8pm;
  - a workout starting after 7pm;
  - caffeine (200 mg or more, logged or from Health);
  - alcohol (logged);
  - a warm bedroom (SIRISAI_HEALTH_BEDROOM_TEMPERATURE_ENTITY over 22 °C,
    10pm to 6am, from Home Assistant);
  - a screen on late (SIRISAI_HEALTH_SCREEN_ENTITY on after 10pm).
  "After a meeting past 8pm you sleep 42 minutes less (6 nights vs 23)."
- **Bedtime** (each evening): wake time is your usual (the median of the
  last two weeks' wake-ups) or earlier when tomorrow's first event needs it
  (SIRISAI_HEALTH_MORNING_BUFFER_MINUTES, 75, before it). Bedtime is wake
  time minus what you need plus half this week's sleep debt (up to an hour)
  and 15 minutes to fall asleep. SIRISAI_HEALTH_WIND_DOWN_MINUTES (45)
  before it, the feed and phone say so, with **Wind down** (the wind_down
  protocol: lights dimmed, a reminder) or, with SIRISAI_HEALTH_WIND_DOWN
  "auto", it just runs.
- **Goals** (`health_set_goal`): "run 1,000 km this year" becomes a SirisAI
  goal tied to a metric; progress is worked out from the data and posted
  with the weekly health note, on pace or behind.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, time, timedelta
from statistics import mean, median
from typing import TYPE_CHECKING, Any

from app.core import clock
from app.health import overview, scores
from app.hub.attention import action, register_action

if TYPE_CHECKING:
    from app.core.siris import SirisAI

logger = logging.getLogger(__name__)

INSIGHTS_KEY = "health_insights"
GOALS_KEY = "health_goals"
WIND_DOWN_KEY = "health_wind_down_last"
MIN_NIGHTS = 4
MIN_MINUTES = 20


def _zone(siris: "SirisAI"):
    return clock.get_zone(siris.settings.timezone)


def _parse(stamp: Any) -> datetime | None:
    raw = str(stamp or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S %z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(raw[:32], fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


# -- getting sick ---------------------------------------------------------------------------


def check_sick(siris: "SirisAI", now: datetime | None = None) -> dict[str, Any] | None:
    on = overview.today(siris) if now is None else now.astimezone(_zone(siris)).date()
    data = overview.days(siris, on, 45)
    found = scores.getting_sick(data, on)
    if not found:
        return None
    if found.get("level") == "early":
        # One day of it (3.82): a heads-up a day before it's sure, without blocking anything.
        siris.attention.raise_item(
            f"health:sick-early:{on.isoformat()}", "notice", "Early signs you may be getting run down", source="health", severity="info",
            body="Today: " + ", ".join(found["today"]) + ". Nothing certain yet; an easy day and an early night help if it is something. "
            "If it's the same tomorrow, SirisAI will say so.",
        )
        _push(siris, "Early signs you may be getting run down", ", ".join(found["today"]) + ". An easy day and an early night help.")
        return found
    evening = _evening_events(siris, on)
    plans = "\n".join(f"- {e['summary']} at {str(e['start'])[11:16]}" for e in evening[:6]) or "Nothing in the calendar this evening."
    actions = [action("acknowledge", "Got it")]
    if siris.calendar is not None and getattr(siris.calendar, "writable", lambda: False)():
        actions.insert(0, action("block", "Block my evening", "primary", confirm="Add a Rest event to your calendar, 5pm to 10pm?"))
    siris.attention.raise_item(
        f"health:sick:{on.isoformat()}",
        "alert",
        "Your body may be fighting something off",
        source="health",
        severity="attention",
        body="Two days running: " + ", ".join(found["today"]) + ". Go easy today: rest, drink water, an early night. "
        "The training plan rests today.\n\nThis evening:\n" + plans,
        actions=actions,
        payload={"day": on.isoformat()},
    )
    _push(siris, "Your body may be fighting something off", "Two days running: " + ", ".join(found["today"]) + ". Take it easy today.")
    return found


def _evening_events(siris: "SirisAI", on: date) -> list[dict[str, Any]]:
    if siris.calendar is None:
        return []
    start = datetime.combine(on, time(17, 0), _zone(siris))
    try:
        return [e for e in siris.calendar.events_between(start, start + timedelta(hours=6)) if len(str(e.get("start") or "")) > 10]
    except Exception:  # noqa: BLE001 - the warning stands without the list
        logger.exception("Reading this evening's calendar failed")
        return []


def _block_evening(siris: "SirisAI", item: dict[str, Any], payload: dict[str, Any], trigger: str) -> tuple[Any, str]:
    day = date.fromisoformat(payload["day"])
    start = datetime.combine(day, time(17, 0), _zone(siris))
    result = siris.calendar.create_event("Rest (feeling unwell)", start, start + timedelta(hours=5))  # type: ignore[union-attr]
    siris.audit.record("calendar_create_event", {"summary": "Rest (feeling unwell)", "start": start.isoformat()}, result=result, trigger=trigger)
    return result, "Evening blocked for rest"


# -- what your sleep goes with --------------------------------------------------------------


def _nightly(siris: "SirisAI", on: date, nights: int) -> dict[str, dict[str, Any]]:
    """Night (keyed by the evening's date) -> sleep that followed and the factors."""
    data = overview.days(siris, on, nights + 2)
    zone = _zone(siris)
    out: dict[str, dict[str, Any]] = {}
    for i in range(1, nights + 1):
        evening = on - timedelta(days=i)
        woke = (evening + timedelta(days=1)).isoformat()
        hours = (data.get(woke) or {}).get("sleep_hours")
        if hours:
            day_m = data.get(evening.isoformat()) or {}
            out[evening.isoformat()] = {"sleep": float(hours), "factors": set(), "caffeine": day_m.get("caffeine_mg"), "alcohol": day_m.get("alcohol_drinks")}
    if not out:
        return out
    first = min(out)
    for night, entry in out.items():
        if (entry["caffeine"] or 0) >= 200:
            entry["factors"].add("caffeine")
        if (entry["alcohol"] or 0) >= 1:
            entry["factors"].add("alcohol")
    for w in siris.health.workouts(first):
        start = _parse(w.get("start"))
        if start and w["date"] in out and (start.astimezone(zone) if start.tzinfo else start).hour >= 19:
            out[w["date"]]["factors"].add("late_workout")
    if siris.calendar is not None:
        try:
            events = siris.calendar.events_between(datetime.combine(date.fromisoformat(first), time(0), zone), datetime.combine(on, time(0), zone))
        except Exception:  # noqa: BLE001
            events = []
        for e in events:
            end = _parse(e.get("end"))
            if end is None or len(str(e.get("end") or "")) <= 10:
                continue
            local = end.astimezone(zone) if end.tzinfo else end
            if local.hour >= 20 and local.date().isoformat() in out:
                out[local.date().isoformat()]["factors"].add("late_meeting")
    _ha_factors(siris, out, zone)
    return out


def _ha_factors(siris: "SirisAI", out: dict[str, dict[str, Any]], zone: Any) -> None:
    ha = getattr(siris, "_home_assistant_client", None)
    temp_id, screen_id = siris.settings.health_bedroom_temperature_entity, siris.settings.health_screen_entity
    if ha is None or not (temp_id or screen_id):
        return
    first = datetime.combine(date.fromisoformat(min(out)), time(12), zone)
    try:
        history = ha.get_history_many([x for x in (temp_id, screen_id) if x], first.astimezone(UTC))
    except Exception:  # noqa: BLE001
        logger.exception("Reading Home Assistant history for sleep insights failed")
        return
    for night in out:
        evening = date.fromisoformat(night)
        if temp_id:
            lo, hi = datetime.combine(evening, time(22), zone), datetime.combine(evening + timedelta(days=1), time(6), zone)
            temps = []
            for when, value in history.get(temp_id, []):
                try:
                    if lo <= when <= hi:
                        temps.append(float(value))
                except ValueError:
                    continue
            if temps and mean(temps) > 22:
                out[night]["factors"].add("warm_bedroom")
        if screen_id:
            lo, hi = datetime.combine(evening, time(22), zone), datetime.combine(evening + timedelta(days=1), time(3), zone)
            states = history.get(screen_id, [])
            before = [v for w, v in states if w <= lo]
            if (before and before[-1] in ("on", "playing")) or any(lo <= w <= hi and v in ("on", "playing") for w, v in states):
                out[night]["factors"].add("late_screen")


FACTOR_TEXT = {
    "late_meeting": "a meeting past 8pm",
    "late_workout": "a workout after 7pm",
    "caffeine": "200 mg or more of caffeine",
    "alcohol": "a drink",
    "warm_bedroom": "a bedroom over 22 °C",
    "late_screen": "the TV on after 10pm",
}


def compute(siris: "SirisAI", now: datetime | None = None, nights: int = 60) -> list[dict[str, Any]]:
    on = overview.today(siris) if now is None else now.astimezone(_zone(siris)).date()
    nightly = _nightly(siris, on, nights)
    found = []
    for factor, text in FACTOR_TEXT.items():
        yes = [n["sleep"] for n in nightly.values() if factor in n["factors"]]
        no = [n["sleep"] for n in nightly.values() if factor not in n["factors"]]
        if len(yes) < MIN_NIGHTS or len(no) < MIN_NIGHTS:
            continue
        diff = round((mean(yes) - mean(no)) * 60)
        if abs(diff) < MIN_MINUTES:
            continue
        found.append({
            "factor": factor, "minutes": diff, "nights_with": len(yes), "nights_without": len(no),
            "text": f"After {text} you sleep {abs(diff)} minutes {'less' if diff < 0 else 'more'} ({len(yes)} nights vs {len(no)}).",
        })
    found.sort(key=lambda f: f["minutes"])
    siris.state.set(INSIGHTS_KEY, {"at": datetime.now(UTC).isoformat(), "insights": found})
    return found


def current(siris: "SirisAI") -> list[dict[str, Any]]:
    return list((siris.state.get(INSIGHTS_KEY) or {}).get("insights") or [])


# -- bedtime -------------------------------------------------------------------------------


def bedtime(siris: "SirisAI", now: datetime | None = None) -> dict[str, Any] | None:
    now = now or datetime.now(UTC)
    zone = _zone(siris)
    local = now.astimezone(zone)
    on = local.date()
    data = overview.days(siris, on, 30)
    if not any(m.get("sleep_hours") for m in data.values()):
        return None
    sleep = scores.sleep(data, siris.health.sleep_times((on - timedelta(days=15)).isoformat()), on)
    wakes = []
    for night in sleep["nights"]:
        end = _parse(night.get("end"))
        if end:
            end = end.astimezone(zone) if end.tzinfo else end
            wakes.append(end.hour * 60 + end.minute)
    usual = int(median(wakes)) if wakes else 7 * 60
    tomorrow = on + timedelta(days=1)
    wake = datetime.combine(tomorrow, time(usual // 60, usual % 60), zone)
    because = "your usual wake-up"
    if siris.calendar is not None:
        lo, hi = datetime.combine(tomorrow, time(4), zone), datetime.combine(tomorrow, time(12), zone)

        def _local(e: dict[str, Any]) -> datetime | None:
            t = _parse(e.get("start")) if len(str(e.get("start") or "")) > 10 else None
            return (t.astimezone(zone) if t.tzinfo else t.replace(tzinfo=zone)) if t else None

        try:
            # Only what *starts* tomorrow morning (4.04): a trip that began today
            # and runs into tomorrow overlaps the window too, and set "bed by 07:28".
            first = next((e for e in sorted(siris.calendar.events_between(lo, hi), key=lambda e: str(e.get("start") or ""))
                          if (t := _local(e)) and lo <= t < hi), None)
        except Exception:  # noqa: BLE001
            first = None
        if first:
            start = _local(first)
            needed = start - timedelta(minutes=siris.settings.health_morning_buffer_minutes) if start else None
            if needed and needed < wake:
                wake, because = needed, f"{first.get('summary') or 'your first event'} at {start:%H:%M}"
    extra = min(1.0, sleep["debt_7d"]) / 2
    target = sleep["need"] + extra
    bed = wake - timedelta(hours=target, minutes=15)
    return {
        "bedtime": bed.strftime("%H:%M"), "bedtime_at": bed.isoformat(), "wake": wake.strftime("%H:%M"),
        "sleep_target_h": round(target, 2), "debt_7d": sleep["debt_7d"], "because": because,
        "text": f"Bed by {bed:%H:%M} for {target:.1f} h before {because} ({wake:%H:%M})"
        + (f", catching up {extra * 60:.0f} min of this week's sleep debt." if extra >= 0.1 else "."),
    }


def wind_down_check(siris: "SirisAI", now: datetime | None = None) -> dict[str, Any] | None:
    mode = siris.settings.health_wind_down
    if mode == "off":
        return None
    now = now or datetime.now(UTC)
    local = now.astimezone(_zone(siris))
    local_day = local.date().isoformat()
    if local.hour < 17:
        return None
    done = siris.state.get(WIND_DOWN_KEY) or {}
    if done.get("day") == local_day and done.get("sent"):
        return None
    if done.get("day") == local_day:
        plan = done.get("plan")  # worked out once an evening, not every tick
    else:
        plan = bedtime(siris, now)
        siris.state.set(WIND_DOWN_KEY, {"day": local_day, "plan": plan, "sent": False})
    if not plan:
        return None
    at = datetime.fromisoformat(plan["bedtime_at"]) - timedelta(minutes=siris.settings.health_wind_down_minutes)
    if not (at <= now < at + timedelta(minutes=30)):
        return None
    siris.state.set(WIND_DOWN_KEY, {"day": local_day, "plan": plan, "sent": True})
    if mode == "auto" and siris.tools.get("run_protocol") is not None:
        siris.run_tool("run_protocol", {"name": "wind_down"}, confirmed=True, trigger="health: wind-down time (SIRISAI_HEALTH_WIND_DOWN=auto)")
        _push(siris, "Time to wind down", plan["text"] + " Lights dimmed.")
        return {"ran": True, **plan}
    actions = [action("dismiss", "Not tonight")]
    if siris.tools.get("run_protocol") is not None:
        actions.insert(0, action("wind_down", "Wind down", "primary"))
    siris.attention.raise_item(f"health:wind-down:{local_day}", "suggestion", "Time to wind down", source="health", severity="info",
                               body=plan["text"], actions=actions)
    _push(siris, "Time to wind down", plan["text"])
    return {"offered": True, **plan}


def _wind_down(siris: "SirisAI", item: dict[str, Any], payload: dict[str, Any], trigger: str) -> tuple[Any, str]:
    result = siris.run_tool("run_protocol", {"name": "wind_down"}, confirmed=True, trigger=trigger)
    return result, "Winding down"


# -- goals ---------------------------------------------------------------------------------

GOAL_METRICS = {"running_km": "km run", "walk_run_km": "km walked or run", "exercise_minutes": "minutes of exercise",
                "steps": "steps", "active_energy_kcal": "kJ of active energy", "mindful_minutes": "mindful minutes"}


def set_goal(siris: "SirisAI", metric: str, target: float, start: str, end: str, title: str | None = None) -> dict[str, Any]:
    if metric not in GOAL_METRICS:
        raise ValueError(f"A goal can track one of: {', '.join(GOAL_METRICS)}")
    date.fromisoformat(start), date.fromisoformat(end)
    shown = target * 4.184 if metric == "active_energy_kcal" else target  # kept in kcal, shown in kJ (SI)
    title = title or f"{shown:,.0f} {GOAL_METRICS[metric]} by {end}"
    goal = siris.goals.create_goal(title, [f"Tracked from health data ({metric}), {start} to {end}"])
    goals = dict(siris.state.get(GOALS_KEY) or {})
    goals[str(goal["id"])] = {"metric": metric, "target": target, "start": start, "end": end, "title": title}
    siris.state.set(GOALS_KEY, goals)
    return {"goal_id": goal["id"], **goals[str(goal["id"])], **progress_for(siris, goals[str(goal["id"])])}


def progress_for(siris: "SirisAI", g: dict[str, Any], on: date | None = None) -> dict[str, Any]:
    on = on or overview.today(siris)
    start, end = date.fromisoformat(g["start"]), date.fromisoformat(g["end"])
    done = sum(v for d, v in siris.health.series(g["metric"], g["start"]) if d <= min(on, end).isoformat())
    span = max(1, (end - start).days + 1)
    elapsed = min(span, max(0, (on - start).days + 1))
    expected = g["target"] * elapsed / span
    left_days = max(0, (end - on).days)
    return {"done": round(done, 1), "expected_by_now": round(expected, 1), "percent": round(done / g["target"] * 100, 1) if g["target"] else None,
            "on_pace": done >= expected, "needed_per_week": round((g["target"] - done) / max(1, left_days) * 7, 1) if left_days else None}


def goals_progress(siris: "SirisAI") -> list[dict[str, Any]]:
    out = []
    for goal_id, g in (siris.state.get(GOALS_KEY) or {}).items():
        out.append({"goal_id": int(goal_id), **g, **progress_for(siris, g)})
    return out


# -- weekly --------------------------------------------------------------------------------


def weekly(siris: "SirisAI", now: datetime | None = None) -> dict[str, Any]:
    """Refresh the sleep insights and post goal progress. (The week goes to the
    second brain's Health note with its own weekly jobs, write_week_note.)"""
    found = compute(siris, now)
    goals = goals_progress(siris)
    for g in goals:
        pace = "on pace" if g["on_pace"] else f"behind: {g['needed_per_week']:,.0f} {GOAL_METRICS[g['metric']]} a week to make it" if g["needed_per_week"] else "behind"
        siris.attention.raise_item(f"health:goal:{g['goal_id']}", "notice", f"{g['title']}: {g['done']:,.0f} so far, {pace}", source="health",
                                   severity="info", body=f"{g['percent']}% done; by now you'd want {g['expected_by_now']:,.0f}.")
    return {"insights": found, "goals": goals}


def write_week_note(siris: "SirisAI", found: list[dict[str, Any]], goals: list[dict[str, Any]], now: datetime | None = None) -> dict[str, Any]:
    on = overview.today(siris) if now is None else now.astimezone(_zone(siris)).date()
    built = overview.build(siris, on)
    data = overview.days(siris, on, 8)
    week = [(on - timedelta(days=i)).isoformat() for i in range(7)]
    readiness = [r["score"] for d in week if (r := overview.morning_readiness(siris, date.fromisoformat(d), overview.days(siris, date.fromisoformat(d), 40)))]
    sleeps = [data[d]["sleep_hours"] for d in week if (data.get(d) or {}).get("sleep_hours")]
    workouts = [w for w in built["activity"]["workouts"] if w["date"] in week]
    lines = []
    if readiness:
        lines.append(f"- Readiness averaged {mean(readiness):.0f} (lowest {min(readiness)})")
    if sleeps:
        lines.append(f"- Slept {mean(sleeps):.1f} h a night; sleep debt {built['sleep']['debt_7d']:.1f} h")
    if workouts:
        lines.append(f"- {len(workouts)} workouts, {sum(w['minutes'] or 0 for w in workouts):.0f} min" + (f", {sum(w['km'] or 0 for w in workouts):.1f} km" if any(w["km"] for w in workouts) else ""))
    if built.get("weight"):
        lines.append(f"- Weight trend {built['weight']['trend']:.1f} kg ({built['weight']['per_week']:+.2f} kg a week)")
    if built.get("getting_sick"):
        lines.append("- Signs of getting sick: " + ", ".join(built["getting_sick"]["today"]))
    lines += [f"- {f['text']}" for f in found[:3]]
    lines += [f"- Goal: {g['title']}: {g['done']:,.0f} ({'on pace' if g['on_pace'] else 'behind'})" for g in goals]
    from app.health import gym

    if line := gym.week_line(siris):
        lines.append(line)
    notes = [n for n in built["logged"]["notes"] if n["kind"] in ("symptom", "medication", "feeling")]
    lines += [f"- {n['day']}: {n['kind']} — {n['text']}" for n in notes[:5]]
    if not lines:
        return {"skipped": "no health data this week"}
    content = f"**Health, week to {on.isoformat()}:**\n" + "\n".join(lines)
    siris.second_brain.learn([{"title": "Health", "content": content, "tags": ["health-snapshot"], "source": "SirisAI health data"}])
    return {"written": "Health", "lines": len(lines)}


def _push(siris: "SirisAI", title: str, message: str) -> None:
    if siris.tools.get("notify") is None:
        return
    try:
        siris.run_tool("notify", {"title": title, "message": message}, trigger="health: insight")
    except Exception:  # noqa: BLE001
        logger.exception("Health push failed")


register_action("health", "block", _block_evening)
register_action("health", "wind_down", _wind_down)


# -- what changed (4.17) ---------------------------------------------------------------------


def explain(siris: "SirisAI", on: date | None = None, days: int = 7) -> dict[str, Any]:
    """Why readiness was low: each of the last `days` mornings' kept score,
    and for the low ones (Recover, or under 50) what pulled it down (its
    weakest contributors) and what the evening before had (a late workout or
    meeting, caffeine, a drink, a warm bedroom, late TV, a hard day's strain)."""
    from app.health import strain as strain_mod

    on = on or overview.today(siris)
    data = overview.days(siris, on, days + 40)
    evenings = _nightly(siris, on + timedelta(days=1), days + 1)
    hist = siris.health.workouts((on - timedelta(days=days + 1)).isoformat())
    hr_max = strain_mod.max_hr(siris, hist)
    mornings = []
    for i in range(days - 1, -1, -1):
        d = on - timedelta(days=i)
        r = overview.morning_readiness(siris, d, overview.days(siris, d, 40))
        if not r:
            continue
        entry: dict[str, Any] = {"date": d.isoformat(), "score": r.get("score"), "band": r.get("band")}
        if r.get("band") == "Recover" or (r.get("score") or 100) < 50:
            entry["why"] = [c["text"] for c in (r.get("contributors") or []) if c.get("score", 100) < 50][:3]
            before = (d - timedelta(days=1)).isoformat()
            factors = sorted(FACTOR_TEXT[f] for f in (evenings.get(before) or {}).get("factors") or set())
            day_strain, _ = strain_mod.day_strain(strain_mod.without_doubles([w for w in hist if w["date"] == before]), {}, hr_max)
            if day_strain >= 14:
                factors.append(f"a hard day (strain {day_strain:g})")
            entry["evening_before"] = factors
        mornings.append(entry)
    scores = [m["score"] for m in mornings if m["score"] is not None]
    low = [m for m in mornings if "why" in m]
    lines = []
    if scores:
        lines.append(f"Readiness averaged {mean(scores):.0f} over the last {len(scores)} mornings.")
    for m in low:
        day = date.fromisoformat(m["date"]).strftime("%A")
        bits = "; ".join(m["why"]) or "several things a little under your usual"
        lines.append(f"{day} was {m['score']} ({m['band']}): {bits}." + (f" The evening before had {', '.join(m['evening_before'])}." if m["evening_before"] else ""))
    if scores and not low:
        lines.append("No low mornings: nothing to explain.")
    common = {}
    for m in low:
        for f in m["evening_before"]:
            common[f] = common.get(f, 0) + 1
    if (top := max(common.items(), key=lambda t: t[1], default=None)) and top[1] >= 2:
        lines.append(f"{top[0][0].upper() + top[0][1:]} came before {top[1]} of the low mornings.")
    return {"text": " ".join(lines) or "Not enough readiness history yet.", "mornings": mornings}


def monthly(siris: "SirisAI", now: datetime | None = None) -> dict[str, Any]:
    """The month's training and health (4.17), written to the second brain's
    Health note on the 1st: readiness, sleep, workouts, the weight trend, and
    SirisGym's lifts and measurements. Kept on this server, like the weekly one."""
    from app.health import gym

    today = overview.today(siris) if now is None else now.astimezone(_zone(siris)).date()
    last = today.replace(day=1) - timedelta(days=1)
    first = last.replace(day=1)
    days = [(first + timedelta(days=i)) for i in range((last - first).days + 1)]
    data = overview.days(siris, last, len(days) + 1)
    lines = []
    ready = [r["score"] for d in days if (r := overview.morning_readiness(siris, d, overview.days(siris, d, 40))) and r.get("score") is not None]
    if ready:
        lines.append(f"- Readiness averaged {mean(ready):.0f} (lowest {min(ready)}, highest {max(ready)})")
    sleeps = [data[d.isoformat()]["sleep_hours"] for d in days if (data.get(d.isoformat()) or {}).get("sleep_hours")]
    if sleeps:
        lines.append(f"- Slept {mean(sleeps):.1f} h a night over {len(sleeps)} nights")
    workouts = [w for w in siris.health.workouts(first.isoformat()) if w["date"] <= last.isoformat()]
    if workouts:
        lines.append(f"- {len(workouts)} workouts, {sum(w['minutes'] or 0 for w in workouts):.0f} min"
                     + (f", {sum(w['km'] or 0 for w in workouts):.1f} km" if any(w["km"] for w in workouts) else ""))
    weights = [(d.isoformat(), data[d.isoformat()]["weight_kg"]) for d in days if (data.get(d.isoformat()) or {}).get("weight_kg")]
    if len(weights) >= 2:
        lines.append(f"- Weight {weights[0][1]:.1f} → {weights[-1][1]:.1f} kg ({weights[-1][1] - weights[0][1]:+.1f})")
    g = gym.latest(siris)
    if g:
        for name, lift in list((g.get("lifts") or {}).items())[:4]:
            pts = [p for p in lift.get("last") or [] if p.get("date", "") <= last.isoformat()]
            if pts:
                lines.append(f"- {name}: estimated 1RM {pts[-1]['e1rm']:g} kg (best {lift.get('best'):g})")
        ms = [m for m in g.get("measurements") or [] if first.isoformat() <= m.get("date", "") <= last.isoformat()]
        if ms:
            m = ms[-1]
            lines.append("- Measured " + ", ".join(f"{k} {v:g} cm" for k, v in m.items() if isinstance(v, (int, float)) and k != "date"))
    try:  # 6.48: the month's workouts from SirisGym itself, set by set
        from app.health import gym_week

        training = gym_week.month_training(siris, first, last)
        if training:
            lines += training["lines"]
    except Exception:  # noqa: BLE001
        logger.debug("No SirisGym detail for the monthly report", exc_info=True)
    if not lines:
        return {"skipped": "no health data last month"}
    content = f"**Health and training, {first:%B %Y}:**\n" + "\n".join(lines)
    # 6.48: also in the attention feed, so SirisOS shows the month's report.
    siris.attention.raise_item(f"health:monthly:{first:%Y-%m}", "notice", f"Your {first:%B} in training and health",
                               source="health-monthly", severity="info", body="\n".join(lines))
    if siris.second_brain is None:
        return {"skipped": "no second brain connected", "month": first.strftime("%Y-%m"), "content": content}
    siris.second_brain.learn([{"title": "Health", "content": content, "tags": ["health-snapshot", "monthly"], "source": "SirisAI health data"}])
    return {"written": "Health", "month": first.strftime("%Y-%m"), "lines": len(lines)}
