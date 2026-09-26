"""Reminders and timers (see app/db/models.py's Reminder).

`parse_when` turns what a person says ("in 10 minutes" is handled by the
tool; "19:00", "7:30pm", or a full ISO date-time here) into a UTC moment,
in SirisAI's configured timezone.
"""

import re
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.core.clock import get_zone
from app.db.models import Reminder

MAX_ATTEMPTS = 3
_CLOCK_RE = re.compile(r"^\s*(\d{1,2})(?::(\d{2}))?\s*([ap]m)?\s*$", re.IGNORECASE)


def parse_when(text: str, tz_name: str, now: datetime | None = None) -> datetime:
    """A moment in UTC. A bare clock time ("19:00", "7:30pm") means its next
    occurrence -- today if still ahead, otherwise tomorrow. A full ISO
    date-time without a zone is read in `tz_name`. Raises ValueError."""
    zone = get_zone(tz_name)
    local_now = (now or datetime.now(UTC)).astimezone(zone)
    match = _CLOCK_RE.match(text)
    if match:
        hour, minute, meridiem = int(match.group(1)), int(match.group(2) or 0), (match.group(3) or "").lower()
        if meridiem == "pm" and hour < 12:
            hour += 12
        elif meridiem == "am" and hour == 12:
            hour = 0
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError(f"'{text}' isn't a valid time of day.")
        due = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if due <= local_now:
            due += timedelta(days=1)
        return due.astimezone(UTC)
    try:
        parsed = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(
            f"Couldn't read '{text}' as a time. Use a clock time like '19:00' or a date-time like '2026-09-27T07:30'."
        ) from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=zone)
    return parsed.astimezone(UTC)


class ReminderStore:
    def __init__(self, session_factory: sessionmaker) -> None:
        self._session_factory = session_factory

    def create(self, message: str, due_at_utc: datetime) -> dict[str, Any]:
        with self._session_factory() as session:
            row = Reminder(message=message, due_at=due_at_utc.astimezone(UTC).replace(tzinfo=None))
            session.add(row)
            session.commit()
            return self._serialize(row)

    def list_pending(self) -> list[dict[str, Any]]:
        with self._session_factory() as session:
            rows = session.scalars(select(Reminder).where(Reminder.status == "pending").order_by(Reminder.due_at)).all()
            return [self._serialize(r) for r in rows]

    def cancel(self, reminder_id: int) -> bool:
        with self._session_factory() as session:
            row = session.get(Reminder, reminder_id)
            if row is None or row.status != "pending":
                return False
            row.status = "cancelled"
            session.commit()
            return True

    def due(self, now_utc: datetime | None = None) -> list[dict[str, Any]]:
        cutoff = (now_utc or datetime.now(UTC)).astimezone(UTC).replace(tzinfo=None)
        with self._session_factory() as session:
            rows = session.scalars(
                select(Reminder).where(Reminder.status == "pending", Reminder.due_at <= cutoff).order_by(Reminder.due_at)
            ).all()
            return [self._serialize(r) for r in rows]

    def mark_sent(self, reminder_id: int) -> None:
        with self._session_factory() as session:
            row = session.get(Reminder, reminder_id)
            if row is not None:
                row.status = "sent"
                row.delivered_at = datetime.now(UTC).replace(tzinfo=None)
                session.commit()

    def record_failure(self, reminder_id: int) -> str:
        """Count a failed delivery; after MAX_ATTEMPTS the reminder is marked
        failed so a broken notifier can't retry it forever."""
        with self._session_factory() as session:
            row = session.get(Reminder, reminder_id)
            if row is None:
                return "gone"
            row.attempts += 1
            if row.attempts >= MAX_ATTEMPTS:
                row.status = "failed"
            session.commit()
            return row.status

    @staticmethod
    def _serialize(row: Reminder) -> dict[str, Any]:
        return {
            "id": row.id,
            "message": row.message,
            "due_at": row.due_at.isoformat(),
            "status": row.status,
            "attempts": row.attempts,
            "created_at": row.created_at.isoformat(),
        }
