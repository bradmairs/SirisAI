"""Routine storage (milestone 29): named, structured routines and whether
each is currently due to run. Same "own short-lived session per call"
pattern as GoalStore/MemoryStore.

Deliberately just storage plus a due-check -- nothing here runs a routine
on its own. See app/routines/runner.py (RoutineRunner) for actually
executing one, and app/db/models.py's Routine for why "due" is
interval-based rather than a real scheduler.
"""

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.db.models import Routine


class RoutineStore:
    def __init__(self, session_factory: sessionmaker) -> None:
        self._session_factory = session_factory

    def create_routine(
        self,
        name: str,
        trigger_description: str,
        interval_minutes: int,
        actions: list[str],
        output_description: str | None = None,
    ) -> dict[str, Any]:
        with self._session_factory() as session:
            routine = Routine(
                name=name,
                trigger_description=trigger_description,
                interval_minutes=interval_minutes,
                actions=json.dumps(actions),
                output_description=output_description,
            )
            session.add(routine)
            session.commit()
            return self._serialize(routine)

    def list_routines(self, enabled_only: bool = False) -> list[dict[str, Any]]:
        with self._session_factory() as session:
            stmt = select(Routine).order_by(Routine.created_at.desc(), Routine.id.desc())
            if enabled_only:
                stmt = stmt.where(Routine.enabled.is_(True))
            return [self._serialize(row) for row in session.scalars(stmt).all()]

    def get_routine(self, routine_id: int) -> dict[str, Any] | None:
        with self._session_factory() as session:
            routine = session.get(Routine, routine_id)
            return self._serialize(routine) if routine is not None else None

    def set_enabled(self, routine_id: int, enabled: bool) -> dict[str, Any] | None:
        with self._session_factory() as session:
            routine = session.get(Routine, routine_id)
            if routine is None:
                return None
            routine.enabled = enabled
            session.commit()
            return self._serialize(routine)

    def due_routines(self, now: datetime | None = None) -> list[dict[str, Any]]:
        """Enabled routines that have never run, or whose interval has
        elapsed since they last did -- what
        POST /siris/routines/run_due actually runs.

        Compares raw datetime columns within one session rather than
        round-tripping through the serialized ISO strings list_routines()
        returns: SQLite silently drops tzinfo on the way back out (a
        `datetime.now(UTC)` written in comes back naive), so `now` is
        normalized to naive-UTC here to match rather than risk a
        naive/aware subtraction blowing up."""
        now = (now or datetime.now(UTC)).replace(tzinfo=None)
        with self._session_factory() as session:
            stmt = select(Routine).where(Routine.enabled.is_(True))
            due = []
            for routine in session.scalars(stmt).all():
                if routine.last_run_at is None or now - routine.last_run_at >= timedelta(
                    minutes=routine.interval_minutes
                ):
                    due.append(self._serialize(routine))
            return due

    def mark_run(self, routine_id: int, run_at: datetime | None = None) -> None:
        with self._session_factory() as session:
            routine = session.get(Routine, routine_id)
            if routine is not None:
                routine.last_run_at = run_at or datetime.now(UTC)
                session.commit()

    @staticmethod
    def _serialize(row: Routine) -> dict[str, Any]:
        return {
            "id": row.id,
            "name": row.name,
            "trigger_description": row.trigger_description,
            "interval_minutes": row.interval_minutes,
            "actions": json.loads(row.actions),
            "output_description": row.output_description,
            "enabled": row.enabled,
            "last_run_at": row.last_run_at.isoformat() if row.last_run_at else None,
            "created_at": row.created_at.isoformat(),
            "updated_at": row.updated_at.isoformat(),
        }
