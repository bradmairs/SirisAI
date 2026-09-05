from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import icalendar
import pytest

from app.calendar.client import CalendarClient
from app.plugins.builtin.calendar import (
    CalendarCheckConflictsTool,
    CalendarCreateEventTool,
    CalendarUpcomingEventsTool,
)


def _ical_with_event(
    summary: str, start: datetime, end: datetime, location: str | None = None
) -> icalendar.Calendar:
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
        client = CalendarClient(
            url="https://cal.example.com", username="u", password="p"
        )
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


def test_events_between_queries_the_given_window_across_all_calendars() -> None:
    fake_calendar = MagicMock()
    fake_calendar.name = "Work"
    fake_calendar.search.return_value = []
    fake_principal = MagicMock()
    fake_principal.calendars.return_value = [fake_calendar]

    with patch("app.calendar.client.caldav.DAVClient") as mock_dav_client_cls:
        mock_dav_client_cls.return_value.principal.return_value = fake_principal
        client = CalendarClient(
            url="https://cal.example.com", username="u", password="p"
        )
        start = datetime(2026, 9, 10, 14, 0, tzinfo=timezone.utc)
        end = datetime(2026, 9, 10, 15, 0, tzinfo=timezone.utc)
        client.events_between(start, end)

    fake_calendar.search.assert_called_once_with(
        start=start, end=end, event=True, expand=True
    )


def test_create_event_adds_to_the_first_calendar_by_default() -> None:
    fake_calendar = MagicMock()
    fake_calendar.name = "Work"
    fake_principal = MagicMock()
    fake_principal.calendars.return_value = [fake_calendar]

    with patch("app.calendar.client.caldav.DAVClient") as mock_dav_client_cls:
        mock_dav_client_cls.return_value.principal.return_value = fake_principal
        client = CalendarClient(
            url="https://cal.example.com", username="u", password="p"
        )
        start = datetime(2026, 9, 10, 14, 0, tzinfo=timezone.utc)
        end = datetime(2026, 9, 10, 15, 0, tzinfo=timezone.utc)
        result = client.create_event("Dentist", start, end, location="Clinic")

    fake_calendar.add_event.assert_called_once_with(
        summary="Dentist", dtstart=start, dtend=end, location="Clinic"
    )
    assert result == {
        "summary": "Dentist",
        "start": "2026-09-10T14:00:00+00:00",
        "end": "2026-09-10T15:00:00+00:00",
        "location": "Clinic",
        "calendar": "Work",
    }


def test_create_event_picks_the_named_calendar() -> None:
    work_calendar = MagicMock()
    work_calendar.name = "Work"
    personal_calendar = MagicMock()
    personal_calendar.name = "Personal"
    fake_principal = MagicMock()
    fake_principal.calendars.return_value = [work_calendar, personal_calendar]

    with patch("app.calendar.client.caldav.DAVClient") as mock_dav_client_cls:
        mock_dav_client_cls.return_value.principal.return_value = fake_principal
        client = CalendarClient(
            url="https://cal.example.com", username="u", password="p"
        )
        start = datetime(2026, 9, 10, 14, 0, tzinfo=timezone.utc)
        end = datetime(2026, 9, 10, 15, 0, tzinfo=timezone.utc)
        result = client.create_event("Dentist", start, end, calendar_name="Personal")

    personal_calendar.add_event.assert_called_once()
    work_calendar.add_event.assert_not_called()
    assert result["calendar"] == "Personal"


def test_create_event_raises_for_an_unknown_calendar_name() -> None:
    fake_calendar = MagicMock()
    fake_calendar.name = "Work"
    fake_principal = MagicMock()
    fake_principal.calendars.return_value = [fake_calendar]

    with patch("app.calendar.client.caldav.DAVClient") as mock_dav_client_cls:
        mock_dav_client_cls.return_value.principal.return_value = fake_principal
        client = CalendarClient(
            url="https://cal.example.com", username="u", password="p"
        )
        start = datetime(2026, 9, 10, 14, 0, tzinfo=timezone.utc)
        end = datetime(2026, 9, 10, 15, 0, tzinfo=timezone.utc)
        with pytest.raises(ValueError, match="Nonexistent"):
            client.create_event("Dentist", start, end, calendar_name="Nonexistent")


def test_check_conflicts_tool_parses_iso_timestamps_and_delegates() -> None:
    fake_client = MagicMock()
    fake_client.events_between.return_value = ["conflict"]

    result = CalendarCheckConflictsTool(fake_client).run(
        start="2026-09-10T14:00:00Z", end="2026-09-10T15:00:00Z"
    )

    assert result == ["conflict"]
    called_start, called_end = fake_client.events_between.call_args.args
    assert called_start == datetime(2026, 9, 10, 14, 0, tzinfo=timezone.utc)
    assert called_end == datetime(2026, 9, 10, 15, 0, tzinfo=timezone.utc)


def test_create_event_tool_parses_iso_timestamps_and_delegates() -> None:
    fake_client = MagicMock()
    fake_client.create_event.return_value = {"summary": "Dentist"}

    result = CalendarCreateEventTool(fake_client).run(
        summary="Dentist",
        start="2026-09-10T14:00:00Z",
        end="2026-09-10T15:00:00Z",
        location="Clinic",
    )

    assert result == {"summary": "Dentist"}
    fake_client.create_event.assert_called_once_with(
        "Dentist",
        datetime(2026, 9, 10, 14, 0, tzinfo=timezone.utc),
        datetime(2026, 9, 10, 15, 0, tzinfo=timezone.utc),
        location="Clinic",
        calendar_name=None,
    )


def test_create_event_tool_dry_run_reports_conflicts_without_creating() -> None:
    fake_client = MagicMock()
    fake_client.events_between.return_value = [{"summary": "Existing meeting"}]

    result = CalendarCreateEventTool(fake_client).dry_run(
        summary="Dentist", start="2026-09-10T14:00:00Z", end="2026-09-10T15:00:00Z"
    )

    fake_client.create_event.assert_not_called()
    assert result["would_create"]["summary"] == "Dentist"
    assert result["conflicts"] == [{"summary": "Existing meeting"}]


def test_create_event_tool_dry_run_reports_no_conflicts_for_a_free_slot() -> None:
    fake_client = MagicMock()
    fake_client.events_between.return_value = []

    result = CalendarCreateEventTool(fake_client).dry_run(
        summary="Dentist", start="2026-09-10T14:00:00Z", end="2026-09-10T15:00:00Z"
    )

    assert result["conflicts"] == []
