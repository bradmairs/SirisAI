from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import icalendar

from app.calendar.client import CalendarClient
from app.plugins.builtin.calendar import CalendarUpcomingEventsTool


def _ical_with_event(summary: str, start: datetime, end: datetime, location: str | None = None) -> icalendar.Calendar:
    event = icalendar.Event()
    event.add("summary", summary)
    event.add("dtstart", start)
    event.add("dtend", end)
    if location:
        event.add("location", location)
    cal = icalendar.Calendar()
    cal.add_component(event)
    return cal


def test_upcoming_events_maps_icalendar_components_and_sorts() -> None:
    later_event = MagicMock()
    later_event.icalendar_instance = _ical_with_event(
        "Later meeting",
        datetime(2026, 9, 6, 9, 0, tzinfo=timezone.utc),
        datetime(2026, 9, 6, 10, 0, tzinfo=timezone.utc),
    )
    sooner_event = MagicMock()
    sooner_event.icalendar_instance = _ical_with_event(
        "Sooner meeting",
        datetime(2026, 9, 5, 9, 0, tzinfo=timezone.utc),
        datetime(2026, 9, 5, 10, 0, tzinfo=timezone.utc),
        location="Office",
    )

    fake_calendar = MagicMock()
    fake_calendar.name = "Work"
    fake_calendar.search.return_value = [later_event, sooner_event]

    fake_principal = MagicMock()
    fake_principal.calendars.return_value = [fake_calendar]

    with patch("app.calendar.client.caldav.DAVClient") as mock_dav_client_cls:
        mock_dav_client_cls.return_value.principal.return_value = fake_principal
        client = CalendarClient(url="https://cal.example.com", username="u", password="p")
        events = client.upcoming_events(days_ahead=7)

    assert events == [
        {
            "summary": "Sooner meeting",
            "start": "2026-09-05T09:00:00+00:00",
            "end": "2026-09-05T10:00:00+00:00",
            "location": "Office",
            "calendar": "Work",
        },
        {
            "summary": "Later meeting",
            "start": "2026-09-06T09:00:00+00:00",
            "end": "2026-09-06T10:00:00+00:00",
            "location": None,
            "calendar": "Work",
        },
    ]
    fake_calendar.search.assert_called_once()
    assert fake_calendar.search.call_args.kwargs["event"] is True


def test_calendar_tool_passes_through_days_ahead() -> None:
    fake_client = MagicMock()
    fake_client.upcoming_events.return_value = []
    CalendarUpcomingEventsTool(fake_client).run(days_ahead=14)
    fake_client.upcoming_events.assert_called_once_with(days_ahead=14)
