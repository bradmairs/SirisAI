"""Capability gap storage (milestone 31). Same "own short-lived session
per call" pattern as GoalStore/RoutineStore.

Deliberately just storage -- there's no separate detection engine here.
The model itself decides when a request needs something it has no tool
for (it already sees the full tool list via available_tool_specs() on
every chat turn that offers tools) and calls report_capability_gap
(app/plugins/builtin/capability_gap.py) when it recognizes one; this
store just makes that recognition persist and stay queryable instead of
evaporating at the end of one chat reply.
"""

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.db.models import CapabilityGap


class CapabilityGapStore:
    def __init__(self, session_factory: sessionmaker) -> None:
        self._session_factory = session_factory

    def report_gap(
        self,
        requested_capability: str,
        suggested_integration: str | None = None,
        suggested_tool_spec: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self._session_factory() as session:
            gap = CapabilityGap(
                requested_capability=requested_capability,
                suggested_integration=suggested_integration,
                suggested_tool_spec=json.dumps(suggested_tool_spec) if suggested_tool_spec is not None else None,
            )
            session.add(gap)
            session.commit()
            return self._serialize(gap)

    def list_gaps(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._session_factory() as session:
            stmt = select(CapabilityGap).order_by(CapabilityGap.created_at.desc(), CapabilityGap.id.desc()).limit(limit)
            return [self._serialize(row) for row in session.scalars(stmt).all()]

    @staticmethod
    def _serialize(row: CapabilityGap) -> dict[str, Any]:
        return {
            "id": row.id,
            "requested_capability": row.requested_capability,
            "suggested_integration": row.suggested_integration,
            "suggested_tool_spec": json.loads(row.suggested_tool_spec) if row.suggested_tool_spec else None,
            "created_at": row.created_at.isoformat(),
        }
