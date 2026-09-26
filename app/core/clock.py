"""The current date/time in SirisAI's configured timezone.

The LLM has no clock: asked the date it confidently answered "October 27,
2023" (found live, 2026-09-26). Everything that shows a time to the user or
the model goes through here so it's consistent and in the right zone.
"""

from datetime import UTC, datetime, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def get_zone(name: str) -> tzinfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return UTC


def now_local(tz_name: str) -> datetime:
    return datetime.now(get_zone(tz_name))


def describe_now(tz_name: str) -> str:
    now = now_local(tz_name)
    return f"{now.strftime('%A %-d %B %Y, %H:%M')} ({tz_name})"


def to_local(iso_utc: str | None, tz_name: str) -> str | None:
    """Convert a stored naive-UTC ISO timestamp to a readable local time."""
    if not iso_utc:
        return None
    try:
        value = datetime.fromisoformat(iso_utc)
    except ValueError:
        return iso_utc
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(get_zone(tz_name)).strftime("%a %-d %b %H:%M")
