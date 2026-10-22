"""Thin wrapper around the `caldav` library for reading upcoming events.
Works with any CalDAV server -- Nextcloud, Radicale, Baikal, iCloud, a
Google-via-CalDAV bridge, etc.

Unlike the other clients in this tool library, this leans on an existing
library (`caldav`) rather than talking to the protocol directly: CalDAV's
PROPFIND/REPORT XML queries and recurrence expansion are exactly the kind
of thing worth trusting to a maintained library instead of hand-rolling,
the same reasoning that has SQLAlchemy and httpx doing the equivalent jobs
elsewhere in this app. Couldn't be verified against a live CalDAV server in
development -- timezone/recurrence edge cases on a specific server are the
most likely place this needs adjustment.
"""

import logging
from datetime import UTC, date, datetime, timedelta
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import caldav
from app.calendar.ics import attendees

logger = logging.getLogger(__name__)


class CalendarClient:
    def __init__(self, url: str, username: str, password: str) -> None:
        self.name = "CalDAV"
        self._dav_client = caldav.DAVClient(
            url=url, username=username, password=password
        )

    def upcoming_events(self, days_ahead: int = 7) -> list[dict[str, Any]]:
        start = datetime.now(UTC)
        return self.events_between(start, start + timedelta(days=days_ahead))

    def events_between(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        """Every event across all calendars that overlaps [start, end) --
        the same query `upcoming_events` uses for "what's coming up", but
        with an arbitrary window instead of always starting now. Also the
        basis for conflict checking (milestone 63): whatever this returns
        for a proposed new event's own [start, end) is exactly what that
        event would conflict with."""
        principal = self._dav_client.principal()

        events: list[dict[str, Any]] = []
        for cal in principal.calendars():
            for event in cal.search(start=start, end=end, event=True, expand=True):
                for component in event.icalendar_instance.walk("VEVENT"):
                    dtstart = component.get("dtstart")
                    dtend = component.get("dtend")
                    location = component.get("location")
                    events.append(
                        {
                            "summary": str(component.get("summary", "")),
                            "start": dtstart.dt.isoformat() if dtstart else None,
                            "end": dtend.dt.isoformat() if dtend else None,
                            "location": str(location) if location else None,
                            "calendar": cal.name,
                            "attendees": attendees(component),
                        }
                    )

        events.sort(key=lambda e: e["start"] or "")
        return events

    def create_event(
        self,
        summary: str,
        start: datetime,
        end: datetime,
        location: str | None = None,
        calendar_name: str | None = None,
    ) -> dict[str, Any]:
        """Creates a new event on `calendar_name` (or the principal's
        first calendar if not given -- most CalDAV setups this app
        targets have exactly one calendar worth writing to). Does not
        check for conflicts itself -- see CalendarCreateEventTool.dry_run(),
        which reports them upfront via `events_between()` so the model (or
        a human confirming) can decide, rather than this silently
        overwriting nothing (CalDAV events don't collide, they just
        double-book) or silently refusing."""
        principal = self._dav_client.principal()
        calendars = principal.calendars()
        if not calendars:
            raise ValueError("No calendars available to create an event in.")

        calendar = calendars[0]
        if calendar_name is not None:
            matches = [cal for cal in calendars if cal.name == calendar_name]
            if not matches:
                raise ValueError(f"No calendar named '{calendar_name}' found.")
            calendar = matches[0]

        kwargs: dict[str, Any] = {"summary": summary, "dtstart": start, "dtend": end}
        if location is not None:
            kwargs["location"] = location
        calendar.add_event(**kwargs)

        return {
            "summary": summary,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "location": location,
            "calendar": calendar.name,
        }


class _Source(Protocol):
    name: str

    def events_between(self, start: datetime, end: datetime) -> list[dict[str, Any]]: ...


class CalendarSet:
    """Every calendar SirisAI reads, as one: a CalDAV account (iCloud,
    Nextcloud...) plus any number of read-only .ics feeds (Google Calendar's
    secret address, see app/calendar/ics.py). Calendars named in `exclude`
    are left out everywhere. A source that fails is skipped and logged, so a
    dead feed never hides the others; only if every source fails does the
    call raise.

    New events go to the CalDAV account; feeds are read-only."""

    def __init__(
        self,
        caldav_client: CalendarClient | None = None,
        feeds: list[_Source] | None = None,
        exclude: str = "",
        timezone: str = "UTC",
    ) -> None:
        self._caldav = caldav_client
        self._sources: list[_Source] = ([caldav_client] if caldav_client else []) + list(feeds or [])
        self._exclude = {e.strip().lower() for e in exclude.split(",") if e.strip()}
        self._zone = ZoneInfo(timezone)

    @property
    def writable(self) -> bool:
        return self._caldav is not None

    def upcoming_events(self, days_ahead: int = 7) -> list[dict[str, Any]]:
        start = datetime.now(UTC)
        return self.events_between(start, start + timedelta(days=days_ahead))

    def events_between(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        errors: list[Exception] = []
        for source in self._sources:
            try:
                events += source.events_between(start, end)
            except Exception as exc:  # one dead calendar never hides the rest
                logger.warning("Calendar %s unavailable: %s", source.name, exc)
                errors.append(exc)
        if errors and len(errors) == len(self._sources):
            raise errors[0]
        events = [self._localise(e) for e in events if str(e.get("calendar") or "").strip().lower() not in self._exclude]
        events.sort(key=self._sort_key)
        return events

    def _localise(self, event: dict[str, Any]) -> dict[str, Any]:
        """Times in SIRISAI_TIMEZONE whatever zone the calendar stored them
        in, so "is it tomorrow?" is a plain date comparison everywhere."""
        out = dict(event)
        for key in ("start", "end"):
            value = out.get(key)
            if isinstance(value, str) and len(value) > 10:
                try:
                    moment = datetime.fromisoformat(value)
                except ValueError:
                    continue
                out[key] = (moment if moment.tzinfo else moment.replace(tzinfo=self._zone)).astimezone(self._zone).isoformat()
        return out

    def _sort_key(self, event: dict[str, Any]) -> datetime:
        """Chronological across sources: all-day dates sort at local
        midnight, and offsets (+10:00 vs Z) are compared as instants, not text."""
        value = event.get("start")
        try:
            if isinstance(value, str) and len(value) <= 10:
                d = date.fromisoformat(value)
                return datetime(d.year, d.month, d.day, tzinfo=self._zone)
            moment = datetime.fromisoformat(str(value))
            return moment if moment.tzinfo else moment.replace(tzinfo=self._zone)
        except ValueError:
            return datetime.max.replace(tzinfo=UTC)

    def create_event(
        self,
        summary: str,
        start: datetime,
        end: datetime,
        location: str | None = None,
        calendar_name: str | None = None,
    ) -> dict[str, Any]:
        if self._caldav is None:
            raise ValueError(
                "Your calendars are connected read-only (calendar feeds). "
                "Set SIRISAI_CALDAV_URL (e.g. iCloud) to add events."
            )
        return self._caldav.create_event(summary, start, end, location=location, calendar_name=calendar_name)
