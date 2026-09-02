"""Long-term memory: what SirisAI has learned about you and remembers for
later. Backed by SQLite for now -- a real (semantic/vector) search backend
can replace `recall`'s substring match later without touching callers.

`MemoryStore` opens its own short-lived DB session per call rather than
using FastAPI's `get_db` dependency, since it's called both from HTTP
routes and from tool invocations that don't have a request in scope.
"""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.db.models import MemoryEntry


class MemoryStore:
    def __init__(self, session_factory: sessionmaker) -> None:
        self._session_factory = session_factory

    def remember(self, content: str) -> dict[str, Any]:
        with self._session_factory() as session:
            entry = MemoryEntry(content=content)
            session.add(entry)
            session.commit()
            session.refresh(entry)
            return self._serialize(entry)

    def recall(self, query: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        with self._session_factory() as session:
            stmt = select(MemoryEntry).order_by(MemoryEntry.created_at.desc()).limit(limit)
            if query:
                stmt = stmt.where(MemoryEntry.content.ilike(f"%{query}%"))
            entries = session.scalars(stmt).all()

            # Usage tracking (milestone 30): every entry actually returned
            # counts as "used", regardless of whether the caller passed a
            # query or just asked for the most recent ones.
            now = datetime.now(UTC)
            for entry in entries:
                entry.recall_count += 1
                entry.last_recalled_at = now
            session.commit()

            return [self._serialize(entry) for entry in entries]

    def list_all(self) -> list[dict[str, Any]]:
        """Every memory, for the self_maintenance_check skill -- unlike
        recall(), does NOT count as a use (it's an inspection pass over
        everything, not the model actually recalling something relevant)."""
        with self._session_factory() as session:
            stmt = select(MemoryEntry).order_by(MemoryEntry.created_at.desc())
            return [self._serialize(entry) for entry in session.scalars(stmt).all()]

    @staticmethod
    def _serialize(entry: MemoryEntry) -> dict[str, Any]:
        return {
            "id": entry.id,
            "content": entry.content,
            "created_at": entry.created_at.isoformat(),
            "recall_count": entry.recall_count,
            "last_recalled_at": entry.last_recalled_at.isoformat() if entry.last_recalled_at else None,
        }
