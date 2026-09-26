"""Conversation persistence: POST /siris/chat's turn-by-turn history.

Like MemoryStore, opens its own short-lived DB session per call rather than
using FastAPI's `get_db` dependency, since callers include plain methods on
SirisAI as well as HTTP routes.
"""

import json
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app.db.models import Conversation, Message


class ConversationStore:
    def __init__(self, session_factory: sessionmaker) -> None:
        self._session_factory = session_factory

    def create_conversation(self) -> str:
        with self._session_factory() as session:
            conversation = Conversation()
            session.add(conversation)
            session.commit()
            return conversation.id

    def conversation_exists(self, conversation_id: str) -> bool:
        with self._session_factory() as session:
            return session.get(Conversation, conversation_id) is not None

    def add_message(
        self,
        conversation_id: str,
        role: str,
        content: str | None = None,
        tool_name: str | None = None,
        tool_arguments: dict[str, Any] | None = None,
    ) -> None:
        with self._session_factory() as session:
            session.add(
                Message(
                    conversation_id=conversation_id,
                    role=role,
                    content=content,
                    tool_name=tool_name,
                    tool_arguments=json.dumps(tool_arguments) if tool_arguments is not None else None,
                )
            )
            session.commit()

    def search_messages(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        """What the user and SirisAI actually said, newest first, for
        'what did we talk about' questions. Tool rows are skipped."""
        like = f"%{query.strip().lower()}%"
        with self._session_factory() as session:
            rows = session.scalars(
                select(Message)
                .where(Message.role.in_(("user", "assistant")), func.lower(Message.content).like(like))
                .order_by(Message.created_at.desc(), Message.id.desc())
                .limit(limit)
            ).all()
            return [
                {
                    "conversation_id": m.conversation_id,
                    "role": m.role,
                    "when": m.created_at.isoformat(),
                    "text": (m.content or "")[:300],
                }
                for m in rows
            ]

    def recent_conversations(self, limit: int = 10) -> list[dict[str, Any]]:
        """Conversations that have at least one user message, newest first,
        each with its opening request -- for a history list."""
        with self._session_factory() as session:
            newest = (
                select(Message.conversation_id, func.max(Message.created_at).label("last_at"), func.count(Message.id).label("n"))
                .where(Message.role.in_(("user", "assistant")))
                .group_by(Message.conversation_id)
                .order_by(func.max(Message.created_at).desc())
                .limit(limit)
            )
            out = []
            for conversation_id, last_at, count in session.execute(newest).all():
                first = session.scalars(
                    select(Message)
                    .where(Message.conversation_id == conversation_id, Message.role == "user")
                    .order_by(Message.id)
                    .limit(1)
                ).first()
                if first is None:
                    continue
                out.append(
                    {
                        "conversation_id": conversation_id,
                        "started_with": (first.content or "")[:120],
                        "last_at": last_at.isoformat(),
                        "messages": count,
                    }
                )
            return out

    def get_messages(self, conversation_id: str) -> list[dict[str, Any]]:
        with self._session_factory() as session:
            stmt = (
                select(Message)
                .where(Message.conversation_id == conversation_id)
                .order_by(Message.created_at, Message.id)
            )
            rows = session.scalars(stmt).all()
            return [
                {
                    "role": row.role,
                    "content": row.content,
                    "tool_name": row.tool_name,
                    "tool_arguments": json.loads(row.tool_arguments) if row.tool_arguments else None,
                    "created_at": row.created_at.isoformat(),
                }
                for row in rows
            ]
