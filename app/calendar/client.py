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
        self._dav_client = caldav.DAVClient(url=url, username=username, password=password)

    def upcoming_events(self, days_ahead: int = 7) -> list[dict[str, Any]]:
        principal = self._dav_client.principal()
        start = datetime.now(UTC)
        end = start + timedelta(days=days_ahead)

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
