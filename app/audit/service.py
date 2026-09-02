"""Tool execution audit log.

Every `SirisAI.run_tool()` call -- successful or not -- gets recorded here:
what was asked, what happened, how long it took. Read via
`GET /siris/activity`. Same "own short-lived session per call" pattern as
MemoryStore/ConversationStore.

Explainable autonomy (milestone 28): also carries trigger/evidence/policy
per entry (see app/db/models.py's ToolExecution), and `find_recent()` lets
the explain_recent_action tool (app/plugins/builtin/audit.py) answer "why
did you restart Plex last night" from this same log instead of a parallel
one -- extending what's already recorded rather than inventing a second
record of autonomous actions.
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
        trigger: str | None = None,
        evidence: Any = None,
        policy: str | None = None,
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
                    trigger=trigger,
                    evidence=json.dumps(evidence) if evidence is not None else None,
                    policy=policy,
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
            return [self._serialize(row) for row in session.scalars(stmt).all()]

    def find_recent(self, tool_name: str, argument_contains: str | None = None, limit: int = 5) -> list[dict[str, Any]]:
        """The most recent entries for one tool, most recent first --
        optionally narrowed to ones whose arguments mention
        `argument_contains` (e.g. 'plex'), for "why did you restart plex"
        rather than every docker_restart_container call ever made. Since
        that filter is a substring match over already-JSON-encoded
        arguments, it's applied in Python after an over-fetch rather than
        in SQL."""
        with self._session_factory() as session:
            stmt = (
                select(ToolExecution)
                .where(ToolExecution.tool_name == tool_name)
                .order_by(ToolExecution.created_at.desc(), ToolExecution.id.desc())
                .limit(limit * 5 if argument_contains else limit)
            )
            entries = [self._serialize(row) for row in session.scalars(stmt).all()]

        if argument_contains:
            entries = [e for e in entries if argument_contains in json.dumps(e["arguments"])]
        return entries[:limit]

    @staticmethod
    def _serialize(row: ToolExecution) -> dict[str, Any]:
        return {
            "id": row.id,
            "conversation_id": row.conversation_id,
            "tool_name": row.tool_name,
            "arguments": json.loads(row.arguments),
            "result": json.loads(row.result) if row.result is not None else None,
            "error": row.error,
            "confirmation_required": row.confirmation_required,
            "duration_ms": row.duration_ms,
            "trigger": row.trigger,
            "evidence": json.loads(row.evidence) if row.evidence is not None else None,
            "policy": row.policy,
            "created_at": row.created_at.isoformat(),
        }
