"""Event memory: things that happened (see app/db/models.py's Event),
optionally tagged to an entity. Lets SirisAI answer "has Plex been playing
up lately?" from actual history instead of just current state. Same "own
short-lived session per call" pattern as MemoryStore.
"""

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.db.models import Event


class EventMemoryStore:
    def __init__(self, session_factory: sessionmaker) -> None:
        self._session_factory = session_factory

    def log_event(self, description: str, entity_name: str | None = None) -> dict[str, Any]:
        with self._session_factory() as session:
            row = Event(description=description, entity_name=entity_name)
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

    @staticmethod
    def _serialize(row: Event) -> dict[str, Any]:
        return {
            "id": row.id,
            "entity_name": row.entity_name,
            "description": row.description,
            "created_at": row.created_at.isoformat(),
        }
