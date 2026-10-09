"""Read-only calendar feeds in iCalendar (.ics) format.

This is how Google Calendar is connected. Google's CalDAV needs OAuth, but
every Google calendar has a "Secret address in iCal format" (Calendar
settings -> Integrate calendar) that serves the whole calendar as one .ics
file. Anyone with the address can read it, so treat it like a password.
The same works for any other published feed (Outlook, Fastmail, a shared
iCloud calendar's webcal:// link).

Recurring events are expanded with `recurring_ical_events` (it already
ships with `caldav`). Feeds are cached for a few minutes so the HUD, the
brief and chat don't each refetch the whole calendar.
"""

from __future__ import annotations

import threading
import time
from datetime import date, datetime, timedelta
from typing import Any, Callable
from zoneinfo import ZoneInfo

import httpx
import icalendar
import recurring_ical_events

MAX_FEED_BYTES = 10 * 1024 * 1024
# Tests swap in an httpx.MockTransport.
transport: httpx.BaseTransport | None = None
CACHE_SECONDS = 5 * 60


class IcsFeedError(Exception):
    pass


class IcsCalendar:
    def __init__(
        self,
        name: str,
        url: str,
        timezone: str = "UTC",
        cache_seconds: float = CACHE_SECONDS,
        fetch: Callable[[str], bytes] | None = None,
    ) -> None:
        self.name = name
        # webcal:// is just https:// with a different hat (Apple and Google both hand these out).
        self.url = "https://" + url[len("webcal://"):] if url.lower().startswith("webcal://") else url
        self._zone = ZoneInfo(timezone)
        self._cache_seconds = cache_seconds
        self._fetch = fetch or _fetch
        self._cached: tuple[float, icalendar.Calendar] | None = None
        self._lock = threading.Lock()

    def _calendar(self) -> icalendar.Calendar:
        with self._lock:
            if self._cached and time.monotonic() - self._cached[0] < self._cache_seconds:
                return self._cached[1]
            try:
                calendar = icalendar.Calendar.from_ical(self._fetch(self.url))
            except IcsFeedError:
                raise
            except Exception as exc:  # malformed feed
                raise IcsFeedError(f"{self.name}: not a calendar feed ({type(exc).__name__})") from exc
            self._cached = (time.monotonic(), calendar)
            return calendar

    def events_between(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        events = []
        # Ask for a day either side, then keep what overlaps once floating
        # times and all-day dates are placed in local time.
        for component in recurring_ical_events.of(self._calendar()).between(start - timedelta(days=1), end + timedelta(days=1)):
            if str(component.get("status", "")).upper() == "CANCELLED":
                continue
            dtstart = component.get("dtstart")
            dtend = component.get("dtend")
            location = component.get("location")
            if not dtstart or not self._overlaps(dtstart.dt, dtend.dt if dtend else dtstart.dt, start, end):
                continue
            events.append(
                {
                    "summary": str(component.get("summary", "")),
                    "start": self._iso(dtstart.dt) if dtstart else None,
                    "end": self._iso(dtend.dt) if dtend else None,
                    "location": str(location) if location else None,
                    "calendar": self.name,
                }
            )
        return events

    def _local(self, value: date | datetime) -> datetime:
        if not isinstance(value, datetime):
            return datetime(value.year, value.month, value.day, tzinfo=self._zone)
        return value if value.tzinfo else value.replace(tzinfo=self._zone)

    def _overlaps(self, first: date | datetime, last: date | datetime, start: datetime, end: datetime) -> bool:
        a, b = self._local(first), self._local(last)
        return a < end and (b > start or (a == b and a >= start))

    def _iso(self, value: date | datetime) -> str:
        if isinstance(value, datetime) and value.tzinfo is None:
            # A "floating" time means local time wherever you are: here, that's SIRISAI_TIMEZONE.
            value = value.replace(tzinfo=self._zone)
        return value.isoformat()


def _fetch(url: str) -> bytes:
    try:
        with httpx.Client(transport=transport, timeout=15.0, follow_redirects=True) as client, \
                client.stream("GET", url, headers={"User-Agent": "SirisAI calendar"}) as r:
            if r.status_code in (401, 403, 404):
                raise IcsFeedError(f"Calendar feed refused ({r.status_code}): check the secret address")
            r.raise_for_status()
            chunks, size = [], 0
            for chunk in r.iter_bytes():
                size += len(chunk)
                if size > MAX_FEED_BYTES:
                    raise IcsFeedError("Calendar feed is larger than 10 MB")
                chunks.append(chunk)
            return b"".join(chunks)
    except httpx.HTTPError as exc:
        raise IcsFeedError(f"Calendar feed unreachable ({type(exc).__name__})") from exc


def parse_feeds(value: str, timezone: str = "UTC") -> list[IcsCalendar]:
    """SIRISAI_CALENDAR_ICS_URLS: one feed per line or comma, each `Label|URL`
    (or a bare URL), e.g. `Gmail|https://calendar.google.com/calendar/ical/.../basic.ics`."""
    feeds = []
    for i, entry in enumerate(e.strip() for e in value.replace("\n", ",").split(",")):
        if not entry:
            continue
        label, _, url = entry.rpartition("|") if "|" in entry else ("", "", entry)
        url = url.strip()
        if not url.lower().startswith(("https://", "http://", "webcal://")):
            continue
        default = "Google Calendar" if "google.com" in url else f"Calendar {i + 1}"
        feeds.append(IcsCalendar(label.strip() or default, url, timezone=timezone))
    return feeds

