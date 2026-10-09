"""Calendar tools over every connected calendar (app/calendar/client.py
CalendarSet): a CalDAV account such as iCloud (SIRISAI_CALDAV_URL) and
read-only .ics feeds such as Google Calendar's secret address
(SIRISAI_CALENDAR_ICS_URLS). Registered when either is set.
"""

from datetime import datetime
from typing import Any

from app.calendar.client import CalendarSet
from app.plugins.base import Tool, ToolPermission, ToolVerification

_TIME_PARAMETER = {
    "type": "string",
    "description": "ISO 8601 date-time, e.g. 2026-09-10T14:00:00Z or 2026-09-10T14:00:00+10:00.",
}


class CalendarUpcomingEventsTool(Tool):
    name = "calendar_upcoming_events"
    description = "List upcoming calendar events across all calendars, soonest first."
    permission = ToolPermission.READ
    parameters = {
        "type": "object",
        "properties": {
            "days_ahead": {
                "type": "integer",
                "description": "How many days ahead to look. Defaults to 7.",
            },
        },
        "required": [],
    }

    def __init__(self, client: CalendarSet) -> None:
        self._client = client

    def run(self, days_ahead: int = 7, **kwargs: Any) -> Any:
        return self._client.upcoming_events(days_ahead=days_ahead)


class CalendarCheckConflictsTool(Tool):
    name = "calendar_check_conflicts"
    description = (
        "List any existing events across all calendars that overlap a given time window -- "
        'for answering "am I free then" or checking before proposing a new event.'
    )
    permission = ToolPermission.READ
    parameters = {
        "type": "object",
        "properties": {"start": _TIME_PARAMETER, "end": _TIME_PARAMETER},
        "required": ["start", "end"],
    }

    def __init__(self, client: CalendarSet) -> None:
        self._client = client

    def run(self, start: str = "", end: str = "", **kwargs: Any) -> Any:
        return self._client.events_between(
            datetime.fromisoformat(start), datetime.fromisoformat(end)
        )


class CalendarCreateEventTool(Tool):
    name = "calendar_create_event"
    description = (
        "Create a new calendar event. Does not refuse or block on an overlapping existing "
        "event -- call dry_run first (or calendar_check_conflicts) to see conflicts before "
        "deciding whether to still go ahead."
    )
    # Reversible (the event can be edited/deleted afterwards) and routine,
    # same "known, undoable action" reasoning as DockerRestartContainerTool
    # -- not CONFIRM_REQUIRED, since dry_run already surfaces the one thing
    # (a conflict) worth pausing over, without a required human-in-the-loop
    # round trip for every event.
    permission = ToolPermission.SAFE_ACTION
    parameters = {
        "type": "object",
        "properties": {
            "summary": {"type": "string", "description": "Event title."},
            "start": _TIME_PARAMETER,
            "end": _TIME_PARAMETER,
            "location": {"type": "string", "description": "Optional event location."},
            "calendar_name": {
                "type": "string",
                "description": "Which calendar to create it on. Defaults to the first available calendar.",
            },
        },
        "required": ["summary", "start", "end"],
    }

    def __init__(self, client: CalendarSet) -> None:
        self._client = client

    def run(
        self,
        summary: str = "",
        start: str = "",
        end: str = "",
        location: str | None = None,
        calendar_name: str | None = None,
        **kwargs: Any,
    ) -> Any:
        return self._client.create_event(
            summary,
            datetime.fromisoformat(start),
            datetime.fromisoformat(end),
            location=location,
            calendar_name=calendar_name,
        )

    def dry_run(
        self,
        summary: str = "",
        start: str = "",
        end: str = "",
        location: str | None = None,
        calendar_name: str | None = None,
        **kwargs: Any,
    ) -> Any:
        start_dt = datetime.fromisoformat(start)
        end_dt = datetime.fromisoformat(end)
        conflicts = self._client.events_between(start_dt, end_dt)
        return {
            "would_create": {
                "summary": summary,
                "start": start_dt.isoformat(),
                "end": end_dt.isoformat(),
                "location": location,
                "calendar": calendar_name,
            },
            "conflicts": conflicts,
        }

    def verify(self, arguments: dict[str, Any], result: Any) -> ToolVerification:
        """Independent check that the event actually exists on the
        calendar afterwards, not just that caldav's `add_event()` call
        didn't raise -- the same "success doesn't mean it actually
        happened" gap `DockerRestartContainerTool.verify()` (milestone 38)
        and `HomeAssistantCallServiceTool.verify()` (65) close elsewhere.
        A CalDAV PUT can return success without the server actually
        persisting something `events_between()` finds again (a UID
        collision silently dropped, a calendar that rejects an unknown
        property)."""
        summary = arguments.get("summary", "")
        start_dt = datetime.fromisoformat(arguments.get("start", ""))
        end_dt = datetime.fromisoformat(arguments.get("end", ""))
        events = self._client.events_between(start_dt, end_dt)
        if any(e["summary"] == summary for e in events):
            return ToolVerification(
                verified=True,
                detail=f"'{summary}' now appears on the calendar in that window.",
            )
        return ToolVerification(
            verified=False,
            detail=f"'{summary}' does not appear on the calendar after creation.",
        )
