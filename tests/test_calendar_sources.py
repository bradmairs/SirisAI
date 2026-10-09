"""Gmail (secret iCal feeds) and iCloud (CalDAV) as one calendar, plus the
Home Assistant media-calendar filter."""

from datetime import UTC, datetime
from unittest.mock import MagicMock

import httpx
import pytest

from app.calendar import ics
from app.calendar.client import CalendarSet
from app.calendar.ics import IcsCalendar, IcsFeedError, parse_feeds
from app.core.config import Settings
from app.core.siris import SirisAI
from app.home_assistant.client import HomeAssistantClient
from app.plugins.builtin.calendar import CalendarCreateEventTool, CalendarUpcomingEventsTool
from app.plugins.builtin.home_assistant import HomeAssistantGetCalendarEventsTool

GMAIL = b"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Google Inc//Google Calendar 70.9054//EN
X-WR-CALNAME:brad@gmail.com
BEGIN:VEVENT
UID:standup@google.com
SUMMARY:Standup
DTSTART;TZID=Australia/Melbourne:20261005T091500
DTEND;TZID=Australia/Melbourne:20261005T093000
RRULE:FREQ=WEEKLY;BYDAY=MO,WE,FR
END:VEVENT
BEGIN:VEVENT
UID:dentist@google.com
SUMMARY:Dentist
LOCATION:Shepparton
DTSTART:20261009T000000Z
DTEND:20261009T010000Z
END:VEVENT
BEGIN:VEVENT
UID:cancelled@google.com
SUMMARY:Cancelled lunch
STATUS:CANCELLED
DTSTART:20261009T020000Z
DTEND:20261009T030000Z
END:VEVENT
BEGIN:VEVENT
UID:floating@google.com
SUMMARY:Gym
DTSTART:20261009T173000
DTEND:20261009T183000
END:VEVENT
BEGIN:VEVENT
UID:allday@google.com
SUMMARY:Bins out
DTSTART;VALUE=DATE:20261009
DTEND;VALUE=DATE:20261010
END:VEVENT
END:VCALENDAR
"""

DAY = (datetime(2026, 10, 8, 13, 0, tzinfo=UTC), datetime(2026, 10, 9, 13, 0, tzinfo=UTC))  # Fri 9 Oct, Melbourne


def _feed(body: bytes = GMAIL, name: str = "Gmail", **kwargs) -> tuple[IcsCalendar, list[str]]:
    calls: list[str] = []

    def fetch(url: str) -> bytes:
        calls.append(url)
        return body

    return IcsCalendar(name, "https://calendar.google.com/calendar/ical/x/private-y/basic.ics",
                       timezone="Australia/Melbourne", fetch=fetch, **kwargs), calls


def test_ics_feed_expands_recurrences_and_skips_cancelled() -> None:
    feed, _ = _feed()
    events = feed.events_between(*DAY)
    by_name = {e["summary"]: e for e in events}
    assert set(by_name) == {"Standup", "Dentist", "Gym", "Bins out"}  # Friday's standup comes from the weekly rule
    assert by_name["Standup"]["start"] == "2026-10-09T09:15:00+11:00"
    assert by_name["Dentist"]["location"] == "Shepparton" and by_name["Dentist"]["calendar"] == "Gmail"
    assert by_name["Gym"]["start"] == "2026-10-09T17:30:00+11:00"  # floating time = local time
    assert by_name["Bins out"]["start"] == "2026-10-09"


def test_ics_feed_is_cached() -> None:
    feed, calls = _feed()
    feed.events_between(*DAY)
    feed.events_between(*DAY)
    assert len(calls) == 1
    fresh, calls = _feed(cache_seconds=0)
    fresh.events_between(*DAY)
    fresh.events_between(*DAY)
    assert len(calls) == 2


def test_a_bad_feed_says_so() -> None:
    feed, _ = _feed(body=b"<html>Not found</html>")
    with pytest.raises(IcsFeedError, match="not a calendar feed"):
        feed.events_between(*DAY)


def test_feed_fetch_refuses_a_wrong_secret(monkeypatch) -> None:
    monkeypatch.setattr(ics, "transport", httpx.MockTransport(lambda r: httpx.Response(404)))
    feed = IcsCalendar("Gmail", "webcal://example.com/cal.ics")
    assert feed.url == "https://example.com/cal.ics"
    with pytest.raises(IcsFeedError, match="check the secret address"):
        feed.events_between(*DAY)


def test_parse_feeds_reads_labels_and_ignores_junk() -> None:
    feeds = parse_feeds("Gmail|https://calendar.google.com/calendar/ical/a/basic.ics,\n"
                        "https://calendar.google.com/calendar/ical/b/basic.ics\nnot a url, Family|webcal://p01.icloud.com/x")
    assert [(f.name, f.url[:27]) for f in feeds] == [
        ("Gmail", "https://calendar.google.com"),
        ("Google Calendar", "https://calendar.google.com"),
        ("Family", "https://p01.icloud.com/x"),
    ]
    assert parse_feeds("") == []


class _Source:
    def __init__(self, name: str, events=None, error: Exception | None = None) -> None:
        self.name, self._events, self._error = name, events or [], error

    def events_between(self, start, end):
        if self._error:
            raise self._error
        return self._events


def test_calendar_set_merges_sources_in_time_order_and_excludes() -> None:
    icloud = _Source("iCloud", [
        {"summary": "Site meeting", "start": "2026-10-09T09:30:00+11:00", "calendar": "Work"},
        {"summary": "Mum's birthday", "start": "2026-10-09", "calendar": "Birthdays"},
    ])
    gmail = _Source("Gmail", [
        {"summary": "Standup", "start": "2026-10-08T22:15:00Z", "calendar": "Gmail"},  # 9:15 am Melbourne
        {"summary": "Bins out", "start": "2026-10-09", "calendar": "Gmail"},
    ])
    cal = CalendarSet(feeds=[icloud, gmail], exclude="birthdays", timezone="Australia/Melbourne")
    events = cal.events_between(*DAY)
    assert [e["summary"] for e in events] == ["Bins out", "Standup", "Site meeting"]
    assert events[1]["start"] == "2026-10-09T09:15:00+11:00"  # UTC from Google, shown in local time


def test_one_dead_calendar_never_hides_the_rest() -> None:
    ok = _Source("Gmail", [{"summary": "Standup", "start": "2026-10-09T09:15:00+11:00", "calendar": "Gmail"}])
    dead = _Source("iCloud", error=ConnectionError("down"))
    assert [e["summary"] for e in CalendarSet(feeds=[dead, ok]).events_between(*DAY)] == ["Standup"]
    with pytest.raises(ConnectionError):
        CalendarSet(feeds=[dead]).events_between(*DAY)


def test_feeds_are_read_only_and_caldav_takes_new_events() -> None:
    read_only = CalendarSet(feeds=[_Source("Gmail")])
    with pytest.raises(ValueError, match="read-only"):
        CalendarCreateEventTool(read_only).run(summary="x", start="2026-10-09T10:00:00+11:00", end="2026-10-09T11:00:00+11:00")
    caldav = MagicMock()
    caldav.name = "CalDAV"
    caldav.create_event.return_value = {"summary": "x", "calendar": "Home"}
    both = CalendarSet(caldav, feeds=[_Source("Gmail")])
    assert both.create_event("x", datetime(2026, 10, 9, tzinfo=UTC), datetime(2026, 10, 9, 1, tzinfo=UTC))["calendar"] == "Home"
    assert CalendarUpcomingEventsTool(both).run(days_ahead=1) == []


def test_calendar_tools_register_with_only_a_google_feed() -> None:
    siris = SirisAI(settings=Settings(calendar_ics_urls="Gmail|https://calendar.google.com/calendar/ical/a/basic.ics"))
    assert siris.tools.get("calendar_upcoming_events") is not None
    assert siris.tools.get("calendar_create_event") is not None  # explains it's read-only rather than vanishing


def _ha(handler) -> HomeAssistantClient:
    return HomeAssistantClient(base_url="http://ha.local:8123", token="t", transport=httpx.MockTransport(handler))


def test_home_assistant_media_calendars_are_left_out_unless_named() -> None:
    asked: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/calendars":
            return httpx.Response(200, json=[
                {"entity_id": "calendar.sonarr", "name": "Sonarr"},
                {"entity_id": "calendar.radarr", "name": "Radarr"},
                {"entity_id": "calendar.family", "name": "Family"},
            ])
        if request.url.path == "/api/states":
            return httpx.Response(200, json=[{"entity_id": "calendar.sonarr", "state": "off", "attributes": {"friendly_name": "Sonarr"}}])
        asked.append(request.url.path.rsplit("/", 1)[-1])
        return httpx.Response(200, json=[{"summary": "Show", "start": {"date": "2026-10-09"}}])

    tool = HomeAssistantGetCalendarEventsTool(_ha(handler), timezone="Australia/Melbourne", exclude="sonarr,radarr")
    tool.run(days=1)
    assert asked == ["calendar.family"]
    asked.clear()
    tool.run(days=1, calendar="calendar.sonarr")
    assert asked == ["calendar.sonarr"]
    assert Settings().home_assistant_calendar_exclude == "sonarr,radarr,lidarr,readarr"
