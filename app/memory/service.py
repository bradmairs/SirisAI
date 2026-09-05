"""Long-term memory: what SirisAI has learned about you and remembers for
later. Backed by SQLite for now -- a real (semantic/vector) search backend
can replace `recall`'s substring match later without touching callers.

`MemoryStore` opens its own short-lived DB session per call rather than
using FastAPI's `get_db` dependency, since it's called both from HTTP
routes and from tool invocations that don't have a request in scope.
"""

import re
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import sessionmaker

from app.core.user_context import get_current_user_id
from app.db.models import MemoryEntry

# Bug fix: recall(query=...) used to require the ENTIRE query to appear as
# one literal substring of a memory's content -- fine for a caller who
# passes a short keyword, but ContextEngine.build() (app/context/engine.py)
# passes the user's whole raw prompt as `query` on every turn, and the
# `recall` tool's own description just says "search text" with no warning
# against a full sentence either. "What's my name?" is never a substring of
# "User's name is Brad", so a real, previously-saved memory silently never
# matched -- surfacing as SirisAI insisting it couldn't recall something it
# had, in fact, been told. Splitting the query into individual significant
# words and matching on ANY of them (not all-or-nothing on the whole
# string) turns a natural-language question into a real search instead of
# a near-guaranteed miss, without changing recall()'s signature or callers.
# Still just substring matching underneath -- no embeddings/semantic
# search, and no relevance ranking beyond the existing recency order -- so
# a short, exact query (a name, a single keyword) still works exactly as
# before, and this only changes behavior for a multi-word query that
# previously matched nothing. "user"/"users" are stopworded alongside the
# usual pronouns/function words for a domain-specific reason: nearly every
# memory here is phrased as a fact *about* "the user" ("User's name is...",
# "The user prefers..."), so the word carries no discriminating signal in
# this app's content -- same non-informative role "my"/"i"/"you" already play.
_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "about",
        "am",
        "be",
        "been",
        "being",
        "but",
        "can",
        "could",
        "did",
        "do",
        "does",
        "for",
        "has",
        "have",
        "had",
        "he",
        "her",
        "hers",
        "him",
        "his",
        "how",
        "i",
        "if",
        "in",
        "is",
        "it",
        "its",
        "know",
        "may",
        "me",
        "might",
        "mine",
        "must",
        "my",
        "no",
        "not",
        "of",
        "on",
        "or",
        "our",
        "please",
        "recall",
        "remember",
        "shall",
        "she",
        "should",
        "so",
        "tell",
        "that",
        "the",
        "their",
        "them",
        "these",
        "this",
        "those",
        "to",
        "us",
        "user",
        "users",
        "was",
        "we",
        "were",
        "what",
        "whats",
        "when",
        "where",
        "which",
        "who",
        "whom",
        "whose",
        "why",
        "will",
        "with",
        "would",
        "you",
        "your",
        "yours",
    }
)
_MIN_TERM_LENGTH = 3


def _search_terms(query: str) -> list[str]:
    words = re.findall(r"[a-z0-9]+", query.lower())
    return [w for w in words if len(w) >= _MIN_TERM_LENGTH and w not in _STOPWORDS]


class MemoryStore:
    def __init__(self, session_factory: sessionmaker) -> None:
        self._session_factory = session_factory

    def remember(self, content: str, *, user_id: str | None = None) -> dict[str, Any]:
        user_id = user_id or get_current_user_id()
        with self._session_factory() as session:
            entry = MemoryEntry(content=content, user_id=user_id)
            session.add(entry)
            session.commit()
            session.refresh(entry)
            return self._serialize(entry)

    def recall(
        self, query: str | None = None, limit: int = 20, *, user_id: str | None = None
    ) -> list[dict[str, Any]]:
        user_id = user_id or get_current_user_id()
        with self._session_factory() as session:
            stmt = (
                select(MemoryEntry)
                .where(MemoryEntry.user_id == user_id)
                .order_by(MemoryEntry.created_at.desc())
                .limit(limit)
            )
            if query:
                terms = _search_terms(query)
                if terms:
                    stmt = stmt.where(
                        or_(*[MemoryEntry.content.ilike(f"%{term}%") for term in terms])
                    )
                else:
                    # Every word was filtered out (a query that's entirely
                    # stopwords/too short, e.g. "who am i") -- fall back to
                    # the old literal-substring behavior rather than
                    # matching everything or nothing.
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

    def list_all(self, *, user_id: str | None = None) -> list[dict[str, Any]]:
        """Every memory belonging to one user, for the self_maintenance_check
        skill -- unlike recall(), does NOT count as a use (it's an
        inspection pass over everything, not the model actually recalling
        something relevant)."""
        user_id = user_id or get_current_user_id()
        with self._session_factory() as session:
            stmt = (
                select(MemoryEntry)
                .where(MemoryEntry.user_id == user_id)
                .order_by(MemoryEntry.created_at.desc())
            )
            return [self._serialize(entry) for entry in session.scalars(stmt).all()]

    @staticmethod
    def _serialize(entry: MemoryEntry) -> dict[str, Any]:
        return {
            "id": entry.id,
            "content": entry.content,
            "created_at": entry.created_at.isoformat(),
            "recall_count": entry.recall_count,
            "last_recalled_at": entry.last_recalled_at.isoformat()
            if entry.last_recalled_at
            else None,
        }
