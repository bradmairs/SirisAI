"""Bedtime check: what to do before bed, and what tomorrow looks like.

For "I'm heading to bed", "goodnight", "wind down": looks at what's still on or
open around the house and at tomorrow morning, and offers suggestions. It
never acts on its own -- it reports, and the user (or SirisAI, when asked)
does the turning off.

Composes existing tools only, best-effort per source: a failing calendar or
forecast doesn't stop the rest.
"""

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from app.core.clock import get_zone
from app.skills.base import Skill

if TYPE_CHECKING:
    from app.core.siris import SirisAI

_MAX_LISTED = 6


def _label(entity: dict[str, Any]) -> str:
    return entity.get("name") or entity["entity_id"]


class BedtimeCheckSkill(Skill):
    name = "bedtime_check"
    description = (
        "Use when the user says they're going to bed, goodnight, or asks what to do before bed. Lists lights still "
        "on, unlocked locks, open covers and doors/windows, and media still playing, plus tomorrow's first calendar "
        "event, tomorrow's weather and reminders due tomorrow morning, with suggestions. Read-only: it doesn't turn "
        "anything off."
    )
    parameters = {"type": "object", "properties": {}, "required": []}

    def run(self, siris: "SirisAI", **kwargs: Any) -> Any:
        result: dict[str, Any] = {"checked": False}
        suggestions: list[str] = []

        if siris.tools.get("home_assistant_list_entities") is not None:
            result["checked"] = True
            lights_on = self._states(siris, "light", "on")
            media_on = self._states(siris, "media_player", "playing")
            unlocked = self._states(siris, "lock", "unlocked")
            covers_open = self._states(siris, "cover", "open")
            result["lights_on"] = [_label(e) for e in lights_on]
            result["media_playing"] = [_label(e) for e in media_on]
            result["unlocked"] = [_label(e) for e in unlocked]
            result["covers_open"] = [_label(e) for e in covers_open]
            if siris.tools.get("house_status_check") is not None:
                try:
                    house = siris.run_tool("house_status_check", {})
                    result["house_warnings"] = house.get("warnings", [])
                except Exception:
                    pass
            if lights_on:
                names = ", ".join(_label(e) for e in lights_on[:_MAX_LISTED])
                suggestions.append(f"Turn off {len(lights_on)} light(s): {names}.")
            if media_on:
                suggestions.append(f"Pause or turn off: {', '.join(_label(e) for e in media_on)}.")
            if unlocked:
                suggestions.append(f"Lock: {', '.join(_label(e) for e in unlocked)}.")
            if covers_open:
                suggestions.append(f"Close: {', '.join(_label(e) for e in covers_open)} (a garage door, if that's one).")
            for warning in result.get("house_warnings", []):
                suggestions.append(warning)

        tomorrow = self._tomorrow(siris)
        result["tomorrow"] = tomorrow
        if tomorrow.get("first_event"):
            suggestions.append(f"First thing tomorrow: {tomorrow['first_event']}.")
        if tomorrow.get("weather"):
            suggestions.append(f"Tomorrow's weather: {tomorrow['weather']}.")
        for reminder in tomorrow.get("reminders", []):
            suggestions.append(f"Reminder tomorrow: {reminder}.")

        result["suggestions"] = suggestions
        result["all_clear"] = result["checked"] and not any(
            result.get(k) for k in ("lights_on", "media_playing", "unlocked", "covers_open", "house_warnings")
        )
        return result

    @staticmethod
    def _states(siris: "SirisAI", domain: str, wanted: str) -> list[dict[str, Any]]:
        try:
            entities = siris.run_tool("home_assistant_list_entities", {"domain": domain})
        except Exception:
            return []
        return [e for e in entities if e.get("state") == wanted]

    @staticmethod
    def _tomorrow(siris: "SirisAI") -> dict[str, Any]:
        out: dict[str, Any] = {}
        zone = get_zone(siris.settings.timezone)
        tomorrow = (datetime.now(zone) + timedelta(days=1)).date()

        if siris.tools.get("home_assistant_get_calendar_events") is not None:
            try:
                events = siris.run_tool("home_assistant_get_calendar_events", {"days": 2})
                dated = [e for e in events if str(e.get("start", "")).startswith(tomorrow.isoformat())]
                timed = sorted((e for e in dated if not e.get("all_day")), key=lambda e: e["start"])
                first = (timed or dated)[0] if dated else None
                if first:
                    when = "all day" if first.get("all_day") else str(first["start"])[11:16]
                    out["first_event"] = f"{first['summary']} ({when})"
            except Exception:
                pass

        if siris.tools.get("weather_forecast") is not None:
            try:
                days = siris.run_tool("weather_forecast", {"days": 2})
                if len(days) > 1:
                    d = days[1]
                    out["weather"] = (
                        f"{d['conditions']}, {d['low_c']}-{d['high_c']}°C, {d['rain_chance_percent']}% chance of rain"
                    )
            except Exception:
                pass

        try:
            end = datetime.combine(tomorrow, datetime.min.time(), tzinfo=zone).replace(hour=10).astimezone(UTC)
            due = [r for r in siris.reminders.list_pending() if datetime.fromisoformat(r["due_at"]).replace(tzinfo=UTC) <= end]
            out["reminders"] = [r["message"] for r in due][:5]
        except Exception:
            pass
        return out
