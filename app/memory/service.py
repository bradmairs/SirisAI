"""Long-term memory: what SirisAI has learned about you and remembers for
later. Backed by SQLite for now -- a real (semantic/vector) search backend
can replace `recall`'s substring match later without touching callers.

`MemoryStore` opens its own short-lived DB session per call rather than
using FastAPI's `get_db` dependency, since it's called both from HTTP
routes and from tool invocations that don't have a request in scope.
"""

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
            return [self._serialize(entry) for entry in entries]

    @staticmethod
    def _serialize(entry: MemoryEntry) -> dict[str, Any]:
        return {"id": entry.id, "content": entry.content, "created_at": entry.created_at.isoformat()}
