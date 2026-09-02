"""Entity memory: facts about named people/things (see app/db/models.py's
EntityFact), looked up per-entity rather than by substring search. Same
"own short-lived session per call" pattern as MemoryStore.

Also the world model (milestone 21): typed relationships between entities
(EntityRelationship), so SirisAI can answer "what depends on X" or "what
would shutting down Y affect" by walking edges, not just recall standalone
facts about one entity at a time.
"""

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.db.models import EntityFact, EntityRelationship


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

    def add_relationship(self, subject_entity: str, relation: str, object_entity: str) -> dict[str, Any]:
        with self._session_factory() as session:
            row = EntityRelationship(subject_entity=subject_entity, relation=relation, object_entity=object_entity)
            session.add(row)
            session.commit()
            return self._serialize_relationship(row)

    def relationships_for(self, entity_name: str) -> dict[str, list[dict[str, Any]]]:
        """Every edge touching `entity_name`, split by direction --
        "outgoing" (entity_name is the subject, e.g. what it RUNS_ON or
        USES) and "incoming" (entity_name is the object, e.g. what
        DEPENDS_ON it -- the reverse lookup that answers "what would
        shutting this down affect")."""
        with self._session_factory() as session:
            outgoing_stmt = (
                select(EntityRelationship)
                .where(EntityRelationship.subject_entity == entity_name)
                .order_by(EntityRelationship.id)
            )
            incoming_stmt = (
                select(EntityRelationship)
                .where(EntityRelationship.object_entity == entity_name)
                .order_by(EntityRelationship.id)
            )
            outgoing = session.scalars(outgoing_stmt).all()
            incoming = session.scalars(incoming_stmt).all()
            return {
                "outgoing": [self._serialize_relationship(row) for row in outgoing],
                "incoming": [self._serialize_relationship(row) for row in incoming],
            }

    def all_relationships(self) -> list[dict[str, Any]]:
        """Every edge in the world model, for introspection/whole-graph views."""
        with self._session_factory() as session:
            stmt = select(EntityRelationship).order_by(EntityRelationship.id)
            return [self._serialize_relationship(row) for row in session.scalars(stmt).all()]

    @staticmethod
    def _serialize(row: EntityFact) -> dict[str, Any]:
        return {
            "id": row.id,
            "entity_name": row.entity_name,
            "fact": row.fact,
            "created_at": row.created_at.isoformat(),
        }

    @staticmethod
    def _serialize_relationship(row: EntityRelationship) -> dict[str, Any]:
        return {
            "id": row.id,
            "subject_entity": row.subject_entity,
            "relation": row.relation,
            "object_entity": row.object_entity,
            "created_at": row.created_at.isoformat(),
        }
