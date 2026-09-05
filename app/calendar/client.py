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

from datetime import UTC, datetime, timedelta
from typing import Any

import caldav


class CalendarClient:
    def __init__(self, url: str, username: str, password: str) -> None:
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
