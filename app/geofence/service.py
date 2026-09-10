"""Geofence reminder storage: reminders triggered by a Home Assistant
entity's zone (see app/db/models.py's GeofenceReminder for the full
concept). Same "own short-lived session per call" pattern as
MemoryStore/GoalStore.

Deliberately just a store -- nothing here checks a real entity's current
state or decides a transition happened. That's
GeofenceReminderCheckSkill (app/skills/builtin/geofence_reminder.py),
composed into POST /siris/proactive/check; this only persists what to
watch for and records the last zone seen, the same "store vs. what acts
on it" split GoalStore already draws with the planner.

Scoped per user, same reasoning as GoalStore: every method's `user_id`
keyword defaults to the current request's resolved user, and a
reminder_id belonging to a different user is treated as not found.
"""

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.core.user_context import get_current_user_id
from app.db.models import GeofenceReminder


class GeofenceReminderStore:
    def __init__(self, session_factory: sessionmaker) -> None:
        self._session_factory = session_factory

    def create(
        self,
        entity_id: str,
        zone: str,
        message: str,
        repeat: bool = False,
        *,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        user_id = user_id or get_current_user_id()
        with self._session_factory() as session:
            reminder = GeofenceReminder(
                entity_id=entity_id,
                zone=zone,
                message=message,
                repeat=repeat,
                user_id=user_id,
            )
            session.add(reminder)
            session.commit()
            session.refresh(reminder)
            return self._serialize(reminder)

    def list_all(
        self, enabled_only: bool = False, *, user_id: str | None = None
    ) -> list[dict[str, Any]]:
        user_id = user_id or get_current_user_id()
        with self._session_factory() as session:
            stmt = (
                select(GeofenceReminder)
                .where(GeofenceReminder.user_id == user_id)
                .order_by(GeofenceReminder.created_at.desc(), GeofenceReminder.id.desc())
            )
            if enabled_only:
                stmt = stmt.where(GeofenceReminder.enabled.is_(True))
            return [self._serialize(row) for row in session.scalars(stmt).all()]

    def set_enabled(
        self, reminder_id: int, enabled: bool, *, user_id: str | None = None
    ) -> dict[str, Any] | None:
        user_id = user_id or get_current_user_id()
        with self._session_factory() as session:
            reminder = session.get(GeofenceReminder, reminder_id)
            if reminder is None or reminder.user_id != user_id:
                return None
            reminder.enabled = enabled
            session.commit()
            return self._serialize(reminder)

    def record_zone(self, reminder_id: int, zone: str, *, disable: bool = False) -> None:
        """Updates the baseline GeofenceReminderCheckSkill compares
        against, and -- for a non-repeating reminder that just fired --
        disables it in the same write, so a genuine one-off reminder
        can't fire twice from a single check-to-check race."""
        with self._session_factory() as session:
            reminder = session.get(GeofenceReminder, reminder_id)
            if reminder is None:
                return
            reminder.last_known_zone = zone
            if disable:
                reminder.enabled = False
            session.commit()

    @staticmethod
    def _serialize(reminder: GeofenceReminder) -> dict[str, Any]:
        return {
            "id": reminder.id,
            "entity_id": reminder.entity_id,
            "zone": reminder.zone,
            "message": reminder.message,
            "repeat": reminder.repeat,
            "enabled": reminder.enabled,
            "last_known_zone": reminder.last_known_zone,
            "created_at": reminder.created_at.isoformat(),
        }
