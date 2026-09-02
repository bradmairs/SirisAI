"""Tool execution audit log.

Every `SirisAI.run_tool()` call -- successful or not -- gets recorded here:
what was asked, what happened, how long it took. Read via
`GET /siris/activity`. Same "own short-lived session per call" pattern as
MemoryStore/ConversationStore.
"""

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.db.models import ToolExecution


class AuditLogStore:
    def __init__(self, session_factory: sessionmaker) -> None:
        self._session_factory = session_factory

    def record(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        result: Any = None,
        error: str | None = None,
        duration_ms: float = 0.0,
        conversation_id: str | None = None,
        confirmation_required: bool = False,
    ) -> None:
        with self._session_factory() as session:
            session.add(
                ToolExecution(
                    conversation_id=conversation_id,
                    tool_name=tool_name,
                    arguments=json.dumps(arguments),
                    result=json.dumps(result) if error is None else None,
                    error=error,
                    confirmation_required=confirmation_required,
                    duration_ms=duration_ms,
                )
            )
            session.commit()

    def list_recent(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._session_factory() as session:
            stmt = (
                select(ToolExecution)
                .order_by(ToolExecution.created_at.desc(), ToolExecution.id.desc())
                .limit(limit)
            )
            rows = session.scalars(stmt).all()
            return [
                {
                    "id": row.id,
                    "conversation_id": row.conversation_id,
                    "tool_name": row.tool_name,
                    "arguments": json.loads(row.arguments),
                    "result": json.loads(row.result) if row.result is not None else None,
                    "error": row.error,
                    "confirmation_required": row.confirmation_required,
                    "duration_ms": row.duration_ms,
                    "created_at": row.created_at.isoformat(),
                }
                for row in rows
            ]
