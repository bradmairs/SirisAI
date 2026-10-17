"""Habit mining: notice the things you do by hand at the same time most days
and offer to automate them.

From the Home Assistant logbook over the last SIRISAI_HABIT_LOOKBACK_DAYS
(14): every manual change (anything Home Assistant attributes to an
automation or script is ignored -- that's already automated) to a light,
switch, fan, media player, cover, climate or input_boolean is grouped by
entity, new state, and weekday vs weekend. For each group, the busiest
40-minute window of the day is found; if it holds that change on at least
60% of the eligible days (and at least SIRISAI_HABIT_MIN_DAYS of them), it's
a habit: "You turn on Kitchen light around 07:10 on weekdays (8 of 10)".

Each suggestion carries a ready-made Home Assistant automation (time
trigger, weekday condition, the same service call). Accepting creates it;
dismissing remembers not to suggest it again. Suggestions are kept in
app_state, keyed by entity + state + day type.
"""

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from statistics import median
from typing import TYPE_CHECKING, Any

from app.core import clock

if TYPE_CHECKING:
    from app.core.siris import SirisAI

STATE_KEY = "habit_suggestions"
_DOMAINS = {"light", "switch", "fan", "media_player", "cover", "climate", "input_boolean"}
_SERVICE_FOR_STATE = {"on": "turn_on", "off": "turn_off", "open": "open_cover", "closed": "close_cover", "playing": "media_play", "heat": "turn_on", "cool": "turn_on"}
_WINDOW_MINUTES = 40
_WEEKDAYS = ["mon", "tue", "wed", "thu", "fri"]
_WEEKEND = ["sat", "sun"]


def _automated(entry: dict[str, Any]) -> bool:
    return entry.get("context_event_type") == "automation_triggered" or entry.get("context_domain") in ("automation", "script")


def mine(logbook: list[dict[str, Any]], now: datetime, tz_name: str, lookback_days: int, min_days: int) -> list[dict[str, Any]]:
    zone = clock.get_zone(tz_name)
    start = (now - timedelta(days=lookback_days)).astimezone(zone).date()
    end = now.astimezone(zone).date()
    days = [start + timedelta(days=i) for i in range((end - start).days)]
    eligible = {"weekday": sum(1 for d in days if d.weekday() < 5), "weekend": sum(1 for d in days if d.weekday() >= 5)}

    automated = {(e.get("entity_id"), e.get("state")) for e in logbook if _automated(e)}
    first_per_day: dict[tuple[str, str, str], dict[Any, int]] = defaultdict(dict)
    names: dict[str, str] = {}
    for entry in logbook:
        entity, state, when = entry.get("entity_id"), entry.get("state"), entry.get("when")
        if not entity or not state or not when or _automated(entry):
            continue
        if entity.split(".", 1)[0] not in _DOMAINS or state not in _SERVICE_FOR_STATE or (entity, state) in automated:
            continue
        local = datetime.fromisoformat(when.replace("Z", "+00:00")).astimezone(zone)
        if not (start <= local.date() < end):
            continue
        day_type = "weekday" if local.weekday() < 5 else "weekend"
        minute = local.hour * 60 + local.minute
        per_day = first_per_day[(entity, state, day_type)]
        per_day[local.date()] = min(per_day.get(local.date(), minute), minute)
        names[entity] = entry.get("name") or entity

    habits = []
    for (entity, state, day_type), per_day in first_per_day.items():
        total = eligible[day_type]
        needed = max(min_days if day_type == "weekday" else max(2, min_days // 2), round(total * 0.6))
        if total == 0 or len(per_day) < needed:
            continue
        minutes = sorted(per_day.values())
        best: list[int] = []
        for i, m in enumerate(minutes):
            window = [x for x in minutes[i:] if x - m <= _WINDOW_MINUTES]
            if len(window) > len(best):
                best = window
        if len(best) < needed:
            continue
        at = int(median(best))
        hhmm = f"{at // 60:02d}:{at % 60:02d}"
        verb = _SERVICE_FOR_STATE[state].replace("_", " ").replace("media play", "start playing")
        habits.append(
            {
                "id": f"{entity}|{state}|{day_type}",
                "entity_id": entity,
                "state": state,
                "day_type": day_type,
                "time": hhmm,
                "days_seen": len(best),
                "days_possible": total,
                "description": f"You {verb} {names[entity]} around {hhmm} on {day_type}s ({len(best)} of {total}).",
                "automation": {
                    "alias": f"{names[entity]} {verb} at {hhmm} ({day_type}s)",
                    "description": "Suggested by SirisAI from your habits.",
                    "trigger": [{"platform": "time", "at": f"{hhmm}:00"}],
                    "condition": [{"condition": "time", "weekday": _WEEKDAYS if day_type == "weekday" else _WEEKEND}],
                    "action": [{"service": f"{entity.split('.', 1)[0]}.{_SERVICE_FOR_STATE[state]}", "target": {"entity_id": entity}}],
                },
            }
        )
    return sorted(habits, key=lambda h: (-h["days_seen"] / h["days_possible"], h["time"]))


def refresh(siris: "SirisAI", now: datetime | None = None) -> list[dict[str, Any]]:
    """Mine the logbook and return only suggestions not seen before
    (new ones are stored as "suggested")."""
    client = siris._home_assistant_client
    if client is None:
        return []
    now = now or datetime.now(UTC)
    days = siris.settings.habit_lookback_days
    logbook = client.get_logbook(since=now - timedelta(days=days))
    found = mine(logbook, now, siris.settings.timezone, days, siris.settings.habit_min_days)
    known: dict[str, Any] = siris.state.get(STATE_KEY) or {}
    fresh = []
    for habit in found:
        if habit["id"] in known:
            continue
        known[habit["id"]] = {**habit, "status": "suggested", "suggested_at": now.isoformat()}
        fresh.append(known[habit["id"]])
    siris.state.set(STATE_KEY, known)
    return fresh


def suggestions(siris: "SirisAI", status: str | None = "suggested") -> list[dict[str, Any]]:
    known: dict[str, Any] = siris.state.get(STATE_KEY) or {}
    return [s for s in known.values() if status is None or s["status"] == status]


def set_status(siris: "SirisAI", suggestion_id: str, status: str, **extra: Any) -> dict[str, Any]:
    known: dict[str, Any] = siris.state.get(STATE_KEY) or {}
    if suggestion_id not in known:
        raise ValueError(f"Unknown habit suggestion: {suggestion_id}")
    known[suggestion_id].update(status=status, **extra)
    siris.state.set(STATE_KEY, known)
    if status in ("accepted", "dismissed") and getattr(siris, "outcomes", None) is not None:
        # The learning loop (app/learning/): whichever way it was answered --
        # a phone button, chat, or the attention feed -- it's counted once.
        siris.outcomes.record("attention:habits", status, key=f"habit:{suggestion_id}", source="habits", ref=f"habit:{suggestion_id}:{status}")
    return known[suggestion_id]
