"""ORM models. Import this module wherever `Base.metadata.create_all` runs
so tables get registered -- `app/core/siris.py` pulls it in transitively via
`app.memory.service`."""

from datetime import UTC, datetime

from sqlalchemy import DateTime, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class MemoryEntry(Base):
    __tablename__ = "memories"

    id: Mapped[int] = mapped_column(primary_key=True)
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(UTC))
