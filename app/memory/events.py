"""Event memory: things that happened (see app/db/models.py's Event),
optionally tagged to an entity. Lets SirisAI answer "has Plex been playing
up lately?" from actual history instead of just current state. Same "own
short-lived session per call" pattern as MemoryStore.

`timing_baseline`/`distinct_recurring_signatures` (event-timing anomaly
detection, a hardening pass) extend this the same way MetricsStore.baseline()
(milestone 27) extended metric samples: real history has to accumulate
before "this is overdue" means anything over a guess, so both return None/
empty until enough of it exists. See app/skills/builtin/event_timing.py
for what actually decides whether something's overdue.
"""

import json
import statistics
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app.db.models import Event


@dataclass
class EventTimingBaseline:
    mean_interval_hours: float
    stdev_interval_hours: float
    sample_count: int
    # Naive-UTC, same SQLite round-trip gotcha as everywhere else here --
    # callers comparing against "now" must normalize the same way
    # (see RoutineStore.due_routines()'s docstring for the full story).
    last_occurred_at: datetime


class EventMemoryStore:
    def __init__(self, session_factory: sessionmaker) -> None:
        self._session_factory = session_factory

    def log_event(
        self,
        description: str,
        entity_name: str | None = None,
        source: str | None = None,
        data: dict[str, Any] | None = None,
        created_at: datetime | None = None,
    ) -> dict[str, Any]:
        """`created_at` defaults to now -- only for backdating an event in
        tests that need to construct a specific occurrence history for
        timing_baseline() (same testability pattern as
        RoutineStore.mark_run()'s `run_at`); real callers never pass it."""
        with self._session_factory() as session:
            row = Event(
                description=description,
                entity_name=entity_name,
                source=source,
                data=json.dumps(data) if data is not None else None,
                **({"created_at": created_at} if created_at is not None else {}),
            )
            session.add(row)
            session.commit()
            return self._serialize(row)

    def recent_events(self, entity_name: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        with self._session_factory() as session:
            stmt = select(Event).order_by(Event.created_at.desc(), Event.id.desc()).limit(limit)
            if entity_name:
                stmt = stmt.where(Event.entity_name == entity_name)
            rows = session.scalars(stmt).all()
            return [self._serialize(row) for row in rows]

    def distinct_recurring_signatures(self, min_samples: int = 5, scan_limit: int = 500) -> list[tuple[str, str | None]]:
        """(description, entity_name) pairs that have occurred at least
        `min_samples` times among the `scan_limit` most recent events --
        the candidate set worth computing a timing baseline for at all.
        Bounded by `scan_limit` so this stays cheap regardless of how much
        event history has accumulated; a description that's only shown up
        once or twice is presumably a one-off, not a recurring signal, so
        it's never even considered."""
        with self._session_factory() as session:
            recent_ids = select(Event.id).order_by(Event.id.desc()).limit(scan_limit).scalar_subquery()
            stmt = (
                select(Event.description, Event.entity_name)
                .where(Event.id.in_(recent_ids))
                .group_by(Event.description, Event.entity_name)
                .having(func.count() >= min_samples)
            )
            return [(row.description, row.entity_name) for row in session.execute(stmt).all()]

    def timing_baseline(
        self, description: str, entity_name: str | None = None, min_samples: int = 5, limit: int = 50
    ) -> EventTimingBaseline | None:
        """Mean/stdev interval (in hours) between the most recent
        occurrences of an event with this exact description (and
        entity_name, including the "no entity" case -- a signature from
        distinct_recurring_signatures() is scoped to both), or None if
        fewer than `min_samples` occurrences exist yet. Same honest
        scope-cut as MetricsStore.baseline(): a fresh signal has no
        history to be meaningfully overdue against."""
        with self._session_factory() as session:
            stmt = select(Event.created_at).where(Event.description == description)
            stmt = stmt.where(Event.entity_name == entity_name) if entity_name is not None else stmt.where(
                Event.entity_name.is_(None)
            )
            stmt = stmt.order_by(Event.created_at.desc()).limit(limit)
            timestamps = sorted(session.scalars(stmt).all())

        if len(timestamps) < min_samples:
            return None

        deltas_hours = [(timestamps[i] - timestamps[i - 1]).total_seconds() / 3600 for i in range(1, len(timestamps))]
        stdev = statistics.stdev(deltas_hours) if len(deltas_hours) > 1 else 0.0
        return EventTimingBaseline(
            mean_interval_hours=statistics.mean(deltas_hours),
            stdev_interval_hours=stdev,
            sample_count=len(timestamps),
            last_occurred_at=timestamps[-1],
        )

    @staticmethod
    def _serialize(row: Event) -> dict[str, Any]:
        return {
            "id": row.id,
            "entity_name": row.entity_name,
            "description": row.description,
            "source": row.source,
            "data": json.loads(row.data) if row.data is not None else None,
            "created_at": row.created_at.isoformat(),
        }
