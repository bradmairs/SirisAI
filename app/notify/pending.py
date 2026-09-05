"""Pending notifications (milestone 72, an "interruption controller"):
non-urgent warnings NotificationManager (26) classified as "notify" tier
land here instead of triggering an immediate push -- "urgent" items still
push right away via the `notify` tool, unchanged. Queued items are folded
into the next daily_briefing (app/skills/builtin/daily_briefing.py) and
cleared once delivered that way, or can be read directly via the
`list_pending_notifications` tool without waiting for the next briefing.

Same "own short-lived session per call" pattern as EventMemoryStore/
MemoryStore.
"""

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.db.models import PendingNotification


class PendingNotificationStore:
    def __init__(self, session_factory: sessionmaker) -> None:
        self._session_factory = session_factory

    def queue(self, message: str, severity: str, category: str) -> dict[str, Any]:
        with self._session_factory() as session:
            row = PendingNotification(
                message=message, severity=severity, category=category
            )
            session.add(row)
            session.commit()
            return self._serialize(row)

    def list_pending(self) -> list[dict[str, Any]]:
        with self._session_factory() as session:
            stmt = select(PendingNotification).order_by(
                PendingNotification.created_at.asc()
            )
            return [self._serialize(row) for row in session.scalars(stmt).all()]

    def clear(self) -> int:
        """Deletes every currently-queued item and returns how many there
        were -- called once a caller (daily_briefing) has actually folded
        them into something the user will see, not before."""
        with self._session_factory() as session:
            rows = session.scalars(select(PendingNotification)).all()
            count = len(rows)
            for row in rows:
                session.delete(row)
            session.commit()
            return count

    @staticmethod
    def _serialize(row: PendingNotification) -> dict[str, Any]:
        return {
            "id": row.id,
            "message": row.message,
            "severity": row.severity,
            "category": row.category,
            "created_at": row.created_at.isoformat(),
        }
