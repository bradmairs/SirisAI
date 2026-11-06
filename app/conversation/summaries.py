"""What we talked about (SirisAI 4.6).

Once a conversation has been quiet for half an hour, the model on this
machine gives it a short title and a sentence or two saying what was asked
and what came of it (never a cloud model: conversations are private). With
no local model, the title is the opening request.

`search` answers "what did I ask you yesterday about the NAS": by words in
the titles, summaries and messages, within a date range, one result per
conversation. GET /siris/conversations/search and the search_conversations
tool both use it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, or_, select

from app.db.models import ConversationSummary, Message
from app.llm import local

if TYPE_CHECKING:
    from app.core.siris import SirisAI

IDLE_MINUTES = 30
BACKFILL_DAYS = 30
PER_RUN = 5
_INSTRUCTIONS = (
    "You summarise a conversation between a person and their home assistant, SirisAI. Reply with JSON only: "
    '{"title": "<at most 8 words>", "summary": "<one or two plain sentences: what they asked and what came of it>"}. '
    "Don't add anything that isn't in the conversation."
)


def _naive(value: datetime) -> datetime:
    return value.astimezone(UTC).replace(tzinfo=None) if value.tzinfo else value


def due(siris: "SirisAI", now: datetime | None = None, limit: int = PER_RUN) -> list[str]:
    """Conversations quiet for IDLE_MINUTES that have no summary yet, or have
    grown since theirs was written, newest first."""
    now = _naive(now or datetime.now(UTC))
    with siris.conversation._session_factory() as session:
        stats = (
            select(Message.conversation_id, func.max(Message.created_at).label("last_at"), func.count(Message.id).label("n"))
            .where(Message.role.in_(("user", "assistant")))
            .group_by(Message.conversation_id)
            .having(func.max(Message.created_at) < now - timedelta(minutes=IDLE_MINUTES))
            .having(func.max(Message.created_at) > now - timedelta(days=BACKFILL_DAYS))
            .order_by(func.max(Message.created_at).desc())
        ).subquery()
        rows = session.execute(
            select(stats.c.conversation_id)
            .outerjoin(ConversationSummary, ConversationSummary.conversation_id == stats.c.conversation_id)
            .where(or_(ConversationSummary.conversation_id.is_(None), ConversationSummary.message_count < stats.c.n))
            .where(stats.c.n >= 2)
            .limit(limit)
        ).all()
    return [r[0] for r in rows]


def _transcript(messages: list[dict[str, Any]], limit: int = 6000) -> str:
    lines = [f"{m['role']}: {(m['content'] or '').strip()}" for m in messages if m["role"] in ("user", "assistant") and m["content"]]
    text = "\n".join(lines)
    return text if len(text) <= limit else text[:limit // 2] + "\n...\n" + text[-limit // 2:]


async def summarize(siris: "SirisAI", conversation_id: str) -> dict[str, Any] | None:
    messages = [m for m in siris.conversation.get_messages(conversation_id) if m["role"] in ("user", "assistant")]
    if not messages:
        return None
    first_user = next((m["content"] or "" for m in messages if m["role"] == "user"), "")
    title, summary = first_user.strip()[:80], ""
    answer = await local.ask_json(siris, _INSTRUCTIONS, _transcript(messages))
    if answer:
        title = str(answer.get("title") or title).strip()[:120]
        summary = str(answer.get("summary") or "").strip()[:600]
    started = datetime.fromisoformat(messages[0]["created_at"])
    last = datetime.fromisoformat(messages[-1]["created_at"])
    with siris.conversation._session_factory() as session:
        row = session.get(ConversationSummary, conversation_id) or ConversationSummary(conversation_id=conversation_id)
        row.title, row.summary = title, summary
        row.started_at, row.last_at, row.message_count = _naive(started), _naive(last), len(messages)
        session.add(row)
        session.commit()
    return {"conversation_id": conversation_id, "title": title, "summary": summary}


async def run(siris: "SirisAI", now: datetime | None = None) -> list[dict[str, Any]]:
    done = []
    for conversation_id in due(siris, now):
        result = await summarize(siris, conversation_id)
        if result:
            done.append(result)
    return done


def _day_bound(value: str | None, end: bool = False) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    if len(value) <= 10 and end:
        parsed += timedelta(days=1)
    return _naive(parsed)


def search(
    siris: "SirisAI", query: str = "", since: str | None = None, until: str | None = None, limit: int = 10
) -> list[dict[str, Any]]:
    """Conversations matching `query` (any word, in the title, summary or a
    message) between `since` and `until` (dates or times; a date includes
    the whole day), newest first, each with its best matching lines."""
    start, end = _day_bound(since), _day_bound(until, end=True)
    words = [w for w in query.lower().split() if len(w) > 1][:6]
    with siris.conversation._session_factory() as session:
        stmt = (
            select(Message.conversation_id, func.min(Message.created_at), func.max(Message.created_at), func.count(Message.id))
            .where(Message.role.in_(("user", "assistant")))
            .group_by(Message.conversation_id)
            .order_by(func.max(Message.created_at).desc())
        )
        if start is not None:
            stmt = stmt.having(func.max(Message.created_at) >= start)
        if end is not None:
            stmt = stmt.having(func.min(Message.created_at) < end)
        candidates = session.execute(stmt.limit(500)).all()
        summaries = {
            s.conversation_id: s
            for s in session.scalars(select(ConversationSummary).where(
                ConversationSummary.conversation_id.in_([c[0] for c in candidates])
            ))
        } if candidates else {}
        out = []
        for conversation_id, first_at, last_at, count in candidates:
            s = summaries.get(conversation_id)
            hits: list[str] = []
            if words:
                about = f"{s.title} {s.summary}".lower() if s else ""
                conditions = [func.lower(Message.content).like(f"%{w}%") for w in words]
                matched = session.scalars(
                    select(Message.content)
                    .where(Message.conversation_id == conversation_id, Message.role.in_(("user", "assistant")), or_(*conditions))
                    .order_by(Message.id)
                    .limit(3)
                ).all()
                hits = [(m or "")[:200] for m in matched]
                if not hits and not any(w in about for w in words):
                    continue
            if s is None:
                opening = session.scalars(
                    select(Message.content).where(Message.conversation_id == conversation_id, Message.role == "user").order_by(Message.id).limit(1)
                ).first()
                title, summary = (opening or "")[:80], ""
            else:
                title, summary = s.title, s.summary
            out.append({
                "conversation_id": conversation_id, "title": title, "summary": summary,
                "started_at": first_at.isoformat(), "last_at": last_at.isoformat(), "messages": count, "matches": hits,
            })
            if len(out) >= limit:
                break
    return out
