"""Capability gap storage (milestone 31). Same "own short-lived session
per call" pattern as GoalStore/RoutineStore.

Deliberately just storage -- there's no separate detection engine here.
The model itself decides when a request needs something it has no tool
for (it already sees the full tool list via available_tool_specs() on
every chat turn that offers tools) and calls report_capability_gap
(app/plugins/builtin/capability_gap.py) when it recognizes one; this
store just makes that recognition persist and stay queryable instead of
evaporating at the end of one chat reply.

A gap's lifecycle (a Jarvis-concept follow-up) is the same "store vs.
what acts on it" split as GoalStore/RoutineStore too: `dismiss()`,
`mark_resolved()`, and `mark_failed()` only ever change a status/detail
column here -- deciding *when* to call them is
DismissCapabilityGapTool's and ResolveCapabilityGapTool's job
(app/plugins/builtin/capability_gap.py), not this store's.
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

    def list_gaps(self, limit: int = 50, status: str | None = None) -> list[dict[str, Any]]:
        with self._session_factory() as session:
            stmt = select(CapabilityGap)
            if status is not None:
                stmt = stmt.where(CapabilityGap.status == status)
            stmt = stmt.order_by(CapabilityGap.created_at.desc(), CapabilityGap.id.desc()).limit(limit)
            return [self._serialize(row) for row in session.scalars(stmt).all()]

    def get_gap(self, gap_id: int) -> dict[str, Any] | None:
        with self._session_factory() as session:
            gap = session.get(CapabilityGap, gap_id)
            return self._serialize(gap) if gap is not None else None

    def dismiss(self, gap_id: int) -> dict[str, Any] | None:
        """No further action wanted on this gap -- closes it without
        involving Hermes at all (e.g. not worth building, or already
        covered some other way)."""
        return self._set_status(gap_id, "dismissed")

    def mark_resolved(self, gap_id: int, detail: str) -> dict[str, Any] | None:
        return self._set_status(gap_id, "resolved", detail)

    def mark_pr_open(self, gap_id: int, detail: str) -> dict[str, Any] | None:
        """Hermes opened a pull request for this gap (a hardening pass,
        2026-09-27: resolve_capability_gap no longer lets Hermes merge to
        main itself -- see that tool's own docstring). Distinct from
        `mark_resolved` (which still means "actually done, on main") so a
        gap in this state isn't silently re-offered as fixed before a human
        has reviewed and merged the PR."""
        return self._set_status(gap_id, "pr_open", detail)

    def mark_failed(self, gap_id: int, detail: str) -> dict[str, Any] | None:
        return self._set_status(gap_id, "failed", detail)

    def _set_status(
        self, gap_id: int, status: str, detail: str | None = None
    ) -> dict[str, Any] | None:
        with self._session_factory() as session:
            gap = session.get(CapabilityGap, gap_id)
            if gap is None:
                return None
            gap.status = status
            if detail is not None:
                gap.resolution_detail = detail
            session.commit()
            session.refresh(gap)
            return self._serialize(gap)

    @staticmethod
    def _serialize(row: CapabilityGap) -> dict[str, Any]:
        return {
            "id": row.id,
            "requested_capability": row.requested_capability,
            "suggested_integration": row.suggested_integration,
            "suggested_tool_spec": json.loads(row.suggested_tool_spec) if row.suggested_tool_spec else None,
            "status": row.status,
            "resolution_detail": row.resolution_detail,
            "created_at": row.created_at.isoformat(),
        }
