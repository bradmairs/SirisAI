"""Calendar tool via CalDAV (app/calendar/client.py). Registered only when
SIRISAI_CALDAV_URL is set. Works with Nextcloud, Radicale, Baikal, iCloud,
or any other CalDAV server.
"""

from typing import Any

from app.calendar.client import CalendarClient
from app.plugins.base import Tool, ToolPermission


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

    def __init__(self, client: CalendarClient) -> None:
        self._client = client

    def run(self, days_ahead: int = 7, **kwargs: Any) -> Any:
        return self._client.upcoming_events(days_ahead=days_ahead)
