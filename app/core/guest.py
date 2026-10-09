"""Guest mode: someone other than the household may be listening, so SirisAI
stops volunteering personal information.

While it's on, tools that read or write personal data (memories, past
conversations, calendars, reminders, goals, email, health, location) aren't
offered to the model at all, the context engine leaves out memories and
recent personal events, and the model is told why. Turned on by the Guest
protocol or set_guest_mode, optionally for a fixed number of hours, after
which it switches itself off.
"""

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.core.siris import SirisAI

STATE_KEY = "guest_mode"

GUEST_NOTE = (
    "Guest mode is on: a visitor may be listening. Don't volunteer personal information about the "
    "household (calendar, reminders, messages, email, health, whereabouts, memories). Home control "
    "and general questions are fine."
)

_PERSONAL_CATEGORIES = {"memory", "calendar", "reminders", "goals", "email", "health", "voice", "parcels", "car"}
_PERSONAL_TOOLS = {
    "home_assistant_get_calendar_events",
    "home_assistant_get_todo_items",
    "create_geofence_reminder",
    "list_geofence_reminders",
    "search_conversations",
    "recent_conversations",
    "meeting_prep",
    "daily_briefing",
    "bedtime_check",
    "departure_check",
    "engineering_projects",  # client project names
}


def status(siris: "SirisAI", now: datetime | None = None) -> dict[str, Any]:
    state = siris.state.get(STATE_KEY) or {"on": False}
    until = state.get("until")
    if state.get("on") and until and datetime.fromisoformat(until) <= (now or datetime.now(UTC)):
        state = {"on": False, "expired_at": until}
        siris.state.set(STATE_KEY, state)
    return state


def is_on(siris: "SirisAI") -> bool:
    return bool(status(siris).get("on"))


def set_mode(siris: "SirisAI", on: bool, hours: float | None = None, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    state: dict[str, Any] = {"on": on, "since": now.isoformat()}
    if on and hours:
        state["until"] = (now + timedelta(hours=hours)).isoformat()
    siris.state.set(STATE_KEY, state)
    return state


def is_personal_tool(name: str, category: str) -> bool:
    return category in _PERSONAL_CATEGORIES or name in _PERSONAL_TOOLS
