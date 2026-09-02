"""Entity memory: facts about named people/things (see app/db/models.py's
EntityFact), looked up per-entity rather than by substring search. Same
"own short-lived session per call" pattern as MemoryStore.
"""

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.db.models import EntityFact


class EntityMemoryStore:
    def __init__(self, session_factory: sessionmaker) -> None:
        self._session_factory = session_factory

    def remember_fact(self, entity_name: str, fact: str) -> dict[str, Any]:
        with self._session_factory() as session:
            row = EntityFact(entity_name=entity_name, fact=fact)
            session.add(row)
            session.commit()
            return self._serialize(row)

    def facts_about(self, entity_name: str, limit: int = 20) -> list[dict[str, Any]]:
        with self._session_factory() as session:
            stmt = (
                select(EntityFact)
                .where(EntityFact.entity_name == entity_name)
                .order_by(EntityFact.created_at.desc(), EntityFact.id.desc())
                .limit(limit)
            )
            rows = session.scalars(stmt).all()
            return [self._serialize(row) for row in rows]

    def list_entities(self) -> list[str]:
        with self._session_factory() as session:
            stmt = select(EntityFact.entity_name).distinct().order_by(EntityFact.entity_name)
            return list(session.scalars(stmt).all())

    @staticmethod
    def _serialize(row: EntityFact) -> dict[str, Any]:
        return {
            "id": row.id,
            "entity_name": row.entity_name,
            "fact": row.fact,
            "created_at": row.created_at.isoformat(),
        }
