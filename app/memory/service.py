"""Long-term memory: what SirisAI has learned about you and remembers for
later. Backed by SQLite.

Semantic recall (SIRISAI_EMBEDDING_PROVIDER): `recall()`'s original plain
substring match had a real, live-hit limitation -- a paraphrased query
("what did I say about routine timing?") shares no literal words with the
memory it should find ("run quietly after 22:00"), so it never matched.
When an `OllamaEmbeddingClient` is configured, `recall()` instead ranks
every memory by embedding cosine similarity to the query and returns the
closest ones above a similarity floor -- a real "what's relevant to this"
search, not string containment. Falls back to word-based OR matching
(`_search_terms`, below) when no embedding client is configured or the
embed call itself fails -- an earlier fix for the same underlying "a
literal-substring query almost never matches a natural-language question"
gap, kept as the fallback rather than replaced outright since it needs no
embedding backend to work at all. A query-less "most recent" call never
needed either path.

The same embeddings back a write-time dedup safety net in `remember()`:
a near-duplicate of something already stored (>= _DEDUP_SIMILARITY) isn't
re-inserted, the existing entry is returned instead. This is a backstop,
not the primary defense -- the primary fix is that automatic memory
extraction (app/cortex/orchestrator.py) already calls `recall` before
deciding whether to `remember`, and now that `recall` actually finds
semantically-equivalent prior entries instead of missing them on wording
alone, that existing "check first" discipline works as originally
intended. Both thresholds were picked empirically (see the commit this
landed in) against a real set of near-duplicate/unrelated memory pairs,
not guessed -- same-fact pairs scored 0.80-0.99, different-fact pairs
0.39-0.64, relevant query-to-memory pairs 0.50-0.67 in a first pass, noise
0.26-0.46. Getting the recall floor right took two live-tested corrections
past that first pass, not just the one pairwise-similarity table: 0.5
turned out too tight -- "what did I say about routine timing" (no "?",
the phrasing a real chat turn actually produces) scored 0.4995 against
its own matching memory, just under the line, a real false negative (the
exact bug this feature exists to fix). Lowering to 0.4 to fix that then
let a genuinely unrelated query ("what is the weather today") through
against homelab-domain memories at ~0.42 -- nomic-embed-text clusters
short, same-domain-but-off-topic text closer together than the first
calibration pass's more topically-scattered pairs suggested, so noise
and signal sit closer together here than that table implies. 0.45 is the
value that held both live corrections at once (clears the weather noise
at ~0.42, keeps the routine-timing match at ~0.50) -- not assumed to be
exactly right forever, the next real miss on either side is the next
data point, not a reason to leave a guessed number in place instead of
whatever the last real test actually showed.

`MemoryStore` opens its own short-lived DB session per call rather than
using FastAPI's `get_db` dependency, since it's called both from HTTP
routes and from tool invocations that don't have a request in scope.
"""

import json
import re
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import sessionmaker

from app.core.user_context import get_current_user_id
from app.db.models import MemoryEntry
from app.llm.embeddings import OllamaEmbeddingClient, cosine_similarity

# A near-duplicate of an existing memory at or above this similarity isn't
# re-inserted by remember() -- see module docstring for how this was
# calibrated. Deliberately high (conservative): a false "these are the
# same fact" merge silently drops genuinely new information, which is
# worse than occasionally leaving a true near-duplicate in place for
# self_maintenance_check to flag instead.
_DEDUP_SIMILARITY = 0.85

# recall()'s ranking floor when using semantic search -- below this, a
# result is noise (an unrelated memory that happens to share some
# vocabulary) rather than a real match. See module docstring for the two
# live corrections behind this specific number.
_RECALL_MIN_SIMILARITY = 0.45

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
    def __init__(
        self,
        session_factory: sessionmaker,
        embedding_client: OllamaEmbeddingClient | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._embedding_client = embedding_client

    def remember(self, content: str, *, user_id: str | None = None) -> dict[str, Any]:
        user_id = user_id or get_current_user_id()
        embedding = self._try_embed(content)

        if embedding is not None:
            duplicate = self._most_similar(
                user_id, embedding, min_similarity=_DEDUP_SIMILARITY
            )
            if duplicate is not None:
                return duplicate

        with self._session_factory() as session:
            entry = MemoryEntry(
                content=content, user_id=user_id, embedding=_encode(embedding)
            )
            session.add(entry)
            session.commit()
            session.refresh(entry)
            return self._serialize(entry)

    def recall(
        self, query: str | None = None, limit: int = 20, *, user_id: str | None = None
    ) -> list[dict[str, Any]]:
        user_id = user_id or get_current_user_id()
        query_embedding = self._try_embed(query) if query else None

        with self._session_factory() as session:
            if query_embedding is not None:
                # Semantic ranking (real cosine similarity) is strictly
                # better than word matching when it's available -- takes
                # priority whenever an embedding client is configured and
                # the query actually embedded successfully.
                entries: list[MemoryEntry] = self._rank_by_similarity(
                    session, user_id, query_embedding, limit
                )
            else:
                stmt = (
                    select(MemoryEntry)
                    .where(MemoryEntry.user_id == user_id)
                    .order_by(MemoryEntry.created_at.desc())
                    .limit(limit)
                )
                if query:
                    # No embeddings configured (or the embed call failed) --
                    # fall back to word-based OR matching rather than
                    # requiring the entire query as one literal substring
                    # (see module docstring for why that used to miss almost
                    # every natural-language question).
                    terms = _search_terms(query)
                    if terms:
                        stmt = stmt.where(
                            or_(
                                *[
                                    MemoryEntry.content.ilike(f"%{term}%")
                                    for term in terms
                                ]
                            )
                        )
                    else:
                        # Every word was filtered out (a query that's entirely
                        # stopwords/too short, e.g. "who am i") -- fall back to
                        # the old literal-substring behavior rather than
                        # matching everything or nothing.
                        stmt = stmt.where(MemoryEntry.content.ilike(f"%{query}%"))
                entries = list(session.scalars(stmt).all())

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

    def find_duplicate_groups(
        self, *, user_id: str | None = None, min_similarity: float = _DEDUP_SIMILARITY
    ) -> list[dict[str, Any]]:
        """Groups of 2+ memories that are the same fact worded differently
        -- for self_maintenance_check's audit, not a live path. Semantic
        similarity when embeddings are configured (this is what actually
        catches the "User's name is Brad" / "user name is Brad" class of
        duplicate -- exact-content matching, the only thing available
        without embeddings, misses it entirely since the strings differ);
        falls back to exact-content grouping otherwise. write-time dedup
        in remember() is the actual prevention against *new* duplicates --
        this exists to surface ones already there, including any saved
        before embeddings were configured at all."""
        user_id = user_id or get_current_user_id()
        with self._session_factory() as session:
            stmt = select(MemoryEntry).where(MemoryEntry.user_id == user_id)
            rows = list(session.scalars(stmt).all())

            if self._embedding_client is None:
                by_content: dict[str, list[int]] = {}
                for row in rows:
                    by_content.setdefault(row.content, []).append(row.id)
                return [
                    {"content": c, "ids": ids}
                    for c, ids in by_content.items()
                    if len(ids) > 1
                ]

            embeddings: dict[int, list[float]] = {}
            for row in rows:
                if row.embedding is not None:
                    embeddings[row.id] = _decode(row.embedding)
                    continue
                computed = self._try_embed(row.content)
                if computed is not None:
                    row.embedding = _encode(computed)
                    embeddings[row.id] = computed
            session.commit()

            parent = {memory_id: memory_id for memory_id in embeddings}

            def find(x: int) -> int:
                while parent[x] != x:
                    x = parent[x]
                return x

            ids = list(embeddings.keys())
            for i in range(len(ids)):
                for j in range(i + 1, len(ids)):
                    if (
                        cosine_similarity(embeddings[ids[i]], embeddings[ids[j]])
                        >= min_similarity
                    ):
                        root_i, root_j = find(ids[i]), find(ids[j])
                        if root_i != root_j:
                            parent[root_j] = root_i

            groups: dict[int, list[int]] = {}
            for memory_id in ids:
                groups.setdefault(find(memory_id), []).append(memory_id)

            content_by_id = {row.id: row.content for row in rows}
            return [
                {"content": content_by_id[group_ids[0]], "ids": sorted(group_ids)}
                for group_ids in groups.values()
                if len(group_ids) > 1
            ]

    def delete(self, memory_id: int, *, user_id: str | None = None) -> bool:
        """Remove one memory (scoped to `user_id`, so one user can't delete
        another's by guessing an id). Returns whether a row was actually
        deleted -- used for one-off duplicate cleanup; not exposed as a
        model tool, deliberately (deciding what to forget is not something
        this hands the LLM yet)."""
        user_id = user_id or get_current_user_id()
        with self._session_factory() as session:
            entry = session.get(MemoryEntry, memory_id)
            if entry is None or entry.user_id != user_id:
                return False
            session.delete(entry)
            session.commit()
            return True

    def _rank_by_similarity(
        self, session: Any, user_id: str, query_embedding: list[float], limit: int
    ) -> list[MemoryEntry]:
        stmt = select(MemoryEntry).where(MemoryEntry.user_id == user_id)
        candidates = list(session.scalars(stmt).all())

        scored: list[tuple[float, MemoryEntry]] = []
        for entry in candidates:
            embedding = self._embedding_for(entry)
            if embedding is None:
                continue
            scored.append((cosine_similarity(query_embedding, embedding), entry))

        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [entry for score, entry in scored if score >= _RECALL_MIN_SIMILARITY][
            :limit
        ]

    def _most_similar(
        self, user_id: str, embedding: list[float], *, min_similarity: float
    ) -> dict[str, Any] | None:
        with self._session_factory() as session:
            stmt = select(MemoryEntry).where(MemoryEntry.user_id == user_id)
            best: tuple[float, MemoryEntry] | None = None
            for entry in session.scalars(stmt).all():
                existing = self._embedding_for(entry)
                if existing is None:
                    continue
                score = cosine_similarity(embedding, existing)
                if best is None or score > best[0]:
                    best = (score, entry)
            if best is not None and best[0] >= min_similarity:
                return self._serialize(best[1])
            return None

    def _embedding_for(self, entry: MemoryEntry) -> list[float] | None:
        """The stored embedding, or -- for a row saved before semantic
        recall was configured (or whose embed call failed at the time) --
        computed and persisted now, so it becomes a real candidate on this
        and every future call instead of being permanently invisible to
        semantic search."""
        if entry.embedding is not None:
            return _decode(entry.embedding)
        embedding = self._try_embed(entry.content)
        if embedding is None:
            return None
        with self._session_factory() as session:
            row = session.get(MemoryEntry, entry.id)
            if row is not None:
                row.embedding = _encode(embedding)
                session.commit()
        entry.embedding = _encode(embedding)
        return embedding

    def _try_embed(self, text: str) -> list[float] | None:
        if self._embedding_client is None:
            return None
        try:
            return self._embedding_client.embed(text)
        except Exception:
            return None

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


def _encode(embedding: list[float] | None) -> str | None:
    return json.dumps(embedding) if embedding is not None else None


def _decode(embedding: str) -> list[float]:
    return json.loads(embedding)
