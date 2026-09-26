"""Entity memory: facts about named people/things (see app/db/models.py's
EntityFact), looked up per-entity rather than by substring search. Same
"own short-lived session per call" pattern as MemoryStore.

Also the world model (milestone 21): typed relationships between entities
(EntityRelationship), so SirisAI can answer "what depends on X" or "what
would shutting down Y affect" by walking edges, not just recall standalone
facts about one entity at a time.
"""

import re
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app.db.models import EntityFact, EntityRelationship


_WORD_RE = re.compile(r"[a-z0-9]+")
# Two facts about the same entity whose word sets overlap this much are the
# same fact worded differently (found live: six near-identical "the office
# bat symbol light is a controllable light" entries after one afternoon).
_FACT_SIMILARITY = 0.7


def _words(text: str) -> set[str]:
    return set(_WORD_RE.findall(text.lower()))


def _similar(a: str, b: str) -> bool:
    wa, wb = _words(a), _words(b)
    if not wa or not wb:
        return a.strip().lower() == b.strip().lower()
    return len(wa & wb) / len(wa | wb) >= _FACT_SIMILARITY


class EntityMemoryStore:
    def __init__(self, session_factory: sessionmaker) -> None:
        self._session_factory = session_factory

    def remember_fact(self, entity_name: str, fact: str) -> dict[str, Any]:
        """Saves a fact about an entity, unless the same fact (or a near-
        identical wording of it) is already saved for that entity -- names
        match case-insensitively ("NAS" and "nas" are one entity). A
        duplicate returns the existing row with `duplicate: true`."""
        entity_name = " ".join(entity_name.split())
        with self._session_factory() as session:
            existing = session.scalars(
                select(EntityFact).where(func.lower(EntityFact.entity_name) == entity_name.lower())
            ).all()
            for row in existing:
                if _similar(row.fact, fact):
                    return {**self._serialize(row), "duplicate": True}
            row = EntityFact(entity_name=entity_name, fact=fact)
            session.add(row)
            session.commit()
            return self._serialize(row)

    def facts_about(self, entity_name: str, limit: int = 20) -> list[dict[str, Any]]:
        with self._session_factory() as session:
            stmt = (
                select(EntityFact)
                .where(func.lower(EntityFact.entity_name) == entity_name.strip().lower())
                .order_by(EntityFact.created_at.desc(), EntityFact.id.desc())
                .limit(limit)
            )
            rows = session.scalars(stmt).all()
            return [self._serialize(row) for row in rows]

    def list_entities(self) -> list[str]:
        with self._session_factory() as session:
            stmt = select(EntityFact.entity_name).distinct().order_by(EntityFact.entity_name)
            return list(session.scalars(stmt).all())

    def delete_fact(self, fact_id: int) -> bool:
        with self._session_factory() as session:
            row = session.get(EntityFact, fact_id)
            if row is None:
                return False
            session.delete(row)
            session.commit()
            return True

    def delete_relationship(self, relationship_id: int) -> bool:
        with self._session_factory() as session:
            row = session.get(EntityRelationship, relationship_id)
            if row is None:
                return False
            session.delete(row)
            session.commit()
            return True

    def add_relationship(self, subject_entity: str, relation: str, object_entity: str) -> dict[str, Any]:
        subject_entity, relation, object_entity = (" ".join(v.split()) for v in (subject_entity, relation, object_entity))
        with self._session_factory() as session:
            duplicate = session.scalars(
                select(EntityRelationship).where(
                    func.lower(EntityRelationship.subject_entity) == subject_entity.lower(),
                    func.lower(EntityRelationship.relation) == relation.lower(),
                    func.lower(EntityRelationship.object_entity) == object_entity.lower(),
                )
            ).first()
            if duplicate is not None:
                return {**self._serialize_relationship(duplicate), "duplicate": True}
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

    def all_entity_names(self) -> list[str]:
        """Every entity name known to the world model -- the union of
        entities with at least one fact (list_entities) and entities that
        only appear as a relationship's subject/object (e.g. "main-server"
        might never get an explicit fact, only edges pointing at it)."""
        with self._session_factory() as session:
            fact_names = set(session.scalars(select(EntityFact.entity_name).distinct()).all())
            subject_names = set(session.scalars(select(EntityRelationship.subject_entity).distinct()).all())
            object_names = set(session.scalars(select(EntityRelationship.object_entity).distinct()).all())
            return sorted(fact_names | subject_names | object_names)

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
