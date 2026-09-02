"""ORM models. Import this module wherever `Base.metadata.create_all` runs
so tables get registered -- `app/core/siris.py` pulls it in transitively via
`app.memory.service`."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class MemoryEntry(Base):
    __tablename__ = "memories"

    id: Mapped[int] = mapped_column(primary_key=True)
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(UTC))


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(primary_key=True, default=lambda: str(uuid.uuid4()))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(UTC))


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"), index=True)
    role: Mapped[str] = mapped_column(Text)  # "user" | "assistant" | "tool"
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    tool_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    tool_arguments: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON-encoded
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(UTC))
