"""Heads-up when you leave: anything left open, what's coming up, and
whether you'll want an umbrella -- in one push, at the moment it's useful.

Runs automatically when presence_check sees the house go from occupied to
empty, and on request ("I'm heading out" in chat, or an iOS Shortcut calling
/siris/ask or the tool endpoint). It gathers:

- Home Assistant: open covers (garage doors, gates), unlocked locks, lights
  left on, media still playing.
- The next calendar event in SIRISAI_DEPARTURE_EVENT_WINDOW_HOURS (3), from
  CalDAV or Home Assistant calendars.
- Today's rain chance from the forecast (warned at 50%+).

With something left open, the push carries a one-tap "Run Away protocol"
button (milestone 109) when SIRISAI_PUBLIC_BASE_URL is set. Travel time is
not included -- no maps integration here.
"""

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from app.core import clock
from app.skills.base import Skill

if TYPE_CHECKING:
    from app.core.siris import SirisAI


def _left_open(siris: "SirisAI") -> list[str]:
    client = siris._home_assistant_client
    if client is None:
        return []
    states = client.list_states()
    name = lambda s: (s.get("attributes") or {}).get("friendly_name") or s["entity_id"]  # noqa: E731
    found = []
    covers = [name(s) for s in states if s["entity_id"].startswith("cover.") and s.get("state") in ("open", "opening")
              and (s.get("attributes") or {}).get("device_class") in ("garage", "gate", "door", None)]
    if covers:
        found.append(f"{', '.join(covers)} {'is' if len(covers) == 1 else 'are'} open")
    locks = [name(s) for s in states if s["entity_id"].startswith("lock.") and s.get("state") == "unlocked"]
    if locks:
        found.append(f"{', '.join(locks)} {'is' if len(locks) == 1 else 'are'} unlocked")
    lights = [name(s) for s in states if s["entity_id"].startswith("light.") and s.get("state") == "on"]
    if lights:
        found.append(f"{len(lights)} light{'s' if len(lights) != 1 else ''} on ({', '.join(lights[:4])})")
    media = [name(s) for s in states if s["entity_id"].startswith("media_player.") and s.get("state") == "playing"]
    if media:
        found.append(f"{', '.join(media)} still playing")
    return found


def _next_event(siris: "SirisAI", now: datetime) -> str | None:
    window = timedelta(hours=siris.settings.departure_event_window_hours)
    events: list[dict[str, Any]] = []
    if siris.tools.get("calendar_upcoming_events") is not None:
        try:
            events = siris.tools.get("calendar_upcoming_events").run(days_ahead=1)  # type: ignore[union-attr]
        except Exception:
            events = []
    upcoming = []
    for event in events:
        start = event.get("start")
        if not isinstance(start, str) or "T" not in start:
            continue  # all-day events aren't "leaving for" anything
        when = datetime.fromisoformat(start.replace("Z", "+00:00"))
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        if now <= when <= now + window:
            upcoming.append((when, event.get("summary") or "an event"))
    if not upcoming:
        return None
    when, summary = min(upcoming)
    minutes = int((when - now).total_seconds() // 60)
    local = when.astimezone(clock.get_zone(siris.settings.timezone)).strftime("%H:%M")
    return f"'{summary}' at {local} starts in {minutes} min"


def _rain(siris: "SirisAI") -> str | None:
    tool = siris.tools.get("weather_forecast")
    if tool is None:
        return None
    try:
        today = tool.run(days=1)[0]
    except Exception:
        return None
    chance = today.get("rain_chance_percent")
    if chance is not None and chance >= 50:
        return f"{chance}% chance of rain today ({today.get('conditions')}) -- take an umbrella"
    return None


class DepartureCheckSkill(Skill):
    name = "departure_check"
    description = (
        "Heads-up for someone leaving home: doors/garage left open, locks unlocked, lights or media left on, the "
        "next calendar event, and whether rain is expected. Use for 'I'm heading out' / 'I'm leaving'."
    )
    parameters = {
        "type": "object",
        "properties": {"notify": {"type": "boolean", "description": "Also send it as a push notification."}},
        "required": [],
    }

    def run(self, siris: "SirisAI", notify: bool = False, now: datetime | None = None, **kwargs: Any) -> Any:
        now = now or datetime.now(UTC)
        left_open = _left_open(siris)
        event = _next_event(siris, now)
        rain = _rain(siris)
        car_line = None
        if siris.tools.get("car_status") is not None:
            try:
                car = siris.tools.get("car_status").run()  # type: ignore[union-attr]
                if car.get("low_battery"):
                    car_line = f"{car['name'][0].upper()}{car['name'][1:]} is at {car['battery']:.0f}% and not plugged in"
            except Exception:
                car_line = None
        parts = []
        if left_open:
            parts.append("Before you go: " + "; ".join(left_open) + ".")
        if event:
            parts.append(f"Coming up: {event}.")
        if rain:
            parts.append(rain[0].upper() + rain[1:] + ".")
        if car_line:
            parts.append(car_line + ".")
        summary = " ".join(parts) or "All good -- everything's closed up and nothing's coming up soon."
        result = {"summary": summary, "left_open": left_open, "next_event": event, "rain": rain, "car": car_line, "notified": False}

        if notify and siris.tools.get("notify") is not None:
            actions = None
            away = siris.tools.get("run_protocol") is not None and left_open and siris.settings.public_base_url
            if away:
                headers = {"Content-Type": "application/json"}
                if siris.settings.api_key:
                    headers["Authorization"] = f"Bearer {siris.settings.api_key}"
                actions = [{
                    "action": "http",
                    "label": "Run Away protocol",
                    "url": f"{siris.settings.public_base_url.rstrip('/')}/siris/tools/run_protocol/run",
                    "method": "POST",
                    "headers": headers,
                    "body": json.dumps({"arguments": {"name": "away"}, "confirmed": True}),
                }]
            siris.run_tool(
                "notify",
                {"title": "SirisAI: heading out", "message": summary, "priority": "high" if left_open else "default", "actions": actions},
                trigger="departure_check: heads-up on leaving",
                evidence=result,
            )
            result["notified"] = True
        return result
