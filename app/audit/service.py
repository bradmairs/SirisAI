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

Self-maintenance (milestone 30): `failure_counts_by_tool()` and
`repeated_planner_sequences()` mine this same log for the
self_maintenance_check skill (app/skills/builtin/self_maintenance.py) --
again, extending what's already recorded rather than a parallel analytics
store.

Deterministic per-tool verification (a hardening pass): also carries
`verified`/`verification_detail` per entry when the tool that ran defines
`Tool.verify()` (app/plugins/base.py) -- an independent check of whether
the call's result actually reflects success, not just that `run()` didn't
raise. Null for the majority of tools that don't define one, same as
trigger/evidence/policy being null for calls with nothing to say there.

Trust ledger (a later hardening pass): `verification_stats_by_tool()`
rolls those per-call `verified` outcomes up into a per-tool success
rate -- see `trust_ledger` (app/plugins/builtin/audit.py) for what
surfaces it.
"""

import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.db.models import ToolExecution

_PLANNER_STEP_TRIGGER = re.compile(r"^planner: step (\d+) of an in-progress plan$")


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
        verified: bool | None = None,
        verification_detail: str | None = None,
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
                    verified=verified,
                    verification_detail=verification_detail,
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

    def failure_counts_by_tool(self, limit: int = 500) -> list[dict[str, Any]]:
        """Tools with at least one failure among the most recent `limit`
        entries, most failures first -- "tools that fail often" AND
        "integrations erroring" for self_maintenance_check (30): this
        codebase doesn't track integration health separately from tool
        execution, so a repeatedly-erroring integration (bad UniFi
        credentials, an unreachable Docker socket) shows up here exactly
        the same way any other flaky tool would, rather than needing a
        second mechanism to say the same thing."""
        with self._session_factory() as session:
            stmt = select(ToolExecution.tool_name, ToolExecution.error).order_by(ToolExecution.id.desc()).limit(limit)
            rows = session.execute(stmt).all()

        counts: dict[str, dict[str, int]] = {}
        for tool_name, error in rows:
            entry = counts.setdefault(tool_name, {"total": 0, "failures": 0})
            entry["total"] += 1
            if error is not None:
                entry["failures"] += 1

        return sorted(
            [{"tool_name": name, **stats} for name, stats in counts.items() if stats["failures"] > 0],
            key=lambda e: e["failures"],
            reverse=True,
        )

    def recent_outcomes(self, since: datetime) -> dict[str, dict[str, int]]:
        """Per tool since `since` (naive UTC): successful runs, and failures
        caused by the environment rather than bad arguments -- what the
        circuit breaker in app/core/health.py needs. Runs held for
        confirmation don't count either way."""
        from app.core.health import is_environmental_error

        with self._session_factory() as session:
            rows = session.execute(
                select(ToolExecution.tool_name, ToolExecution.error, ToolExecution.confirmation_required).where(
                    ToolExecution.created_at >= since
                )
            ).all()
        out: dict[str, dict[str, int]] = {}
        for name, error, needed_confirmation in rows:
            if needed_confirmation:
                continue
            entry = out.setdefault(name, {"successes": 0, "environmental_failures": 0})
            if error is None:
                entry["successes"] += 1
            elif is_environmental_error(error):
                entry["environmental_failures"] += 1
        return out

    def verification_stats_by_tool(self, limit: int = 500, days: int | None = None) -> list[dict[str, Any]]:
        """Aggregate Tool.verify() outcomes per tool among the most
        recent `limit` executions -- a trust ledger (a hardening pass):
        this codebase already records verified/verification_detail per
        call, but nothing before this rolled that up into "which tools'
        real-world verification is actually succeeding," the same
        extend-what's-already-recorded reasoning failure_counts_by_tool()
        uses for failures. Only tools that have attempted verify() at
        least once appear -- a tool with no verify() at all (most of
        them; verified stays null every call) or one that's simply never
        run has nothing meaningful to report. Sorted worst success rate
        first, so a tool whose verify() is failing more than it's
        passing surfaces without needing to already know its name."""
        with self._session_factory() as session:
            stmt = select(ToolExecution.tool_name, ToolExecution.verified)
            if days is not None:
                # Old failures from a since-fixed bug shouldn't drag today's
                # score down (found live: 25% for Home Assistant from
                # pre-fix false negatives).
                stmt = stmt.where(ToolExecution.created_at >= datetime.now(UTC).replace(tzinfo=None) - timedelta(days=days))
            rows = session.execute(stmt.order_by(ToolExecution.id.desc()).limit(limit)).all()

        counts: dict[str, dict[str, int]] = {}
        for tool_name, verified in rows:
            if verified is None:
                continue
            entry = counts.setdefault(tool_name, {"verified_true": 0, "verified_false": 0})
            entry["verified_true" if verified else "verified_false"] += 1

        stats = [
            {
                "tool_name": tool_name,
                "attempts": c["verified_true"] + c["verified_false"],
                "verified_true": c["verified_true"],
                "verified_false": c["verified_false"],
                "success_rate": c["verified_true"] / (c["verified_true"] + c["verified_false"]),
            }
            for tool_name, c in counts.items()
        ]
        return sorted(stats, key=lambda entry: entry["success_rate"])

    def repeated_planner_sequences(self, min_occurrences: int = 3, limit: int = 2000) -> list[dict[str, Any]]:
        """Tool-call sequences the planner (24) has repeated at least
        `min_occurrences` times -- a concrete, data-backed "you've asked
        for this N times, want a skill for it?" signal instead of a vague
        one. A "sequence" is one plan's full ordered list of tool names;
        plan boundaries are inferred from the trigger field's own step
        counter (see Planner) resetting to 1, or the conversation
        changing -- there's no separate plan-id in the audit log, so this
        is a heuristic over trigger text and conversation_id, not a
        stored concept of "a plan" in its own right."""
        with self._session_factory() as session:
            stmt = (
                select(ToolExecution.conversation_id, ToolExecution.tool_name, ToolExecution.trigger)
                .order_by(ToolExecution.id)
                .limit(limit)
            )
            rows = session.execute(stmt).all()

        plans: list[list[str]] = []
        current_plan: list[str] = []
        current_conversation_id: object = object()  # sentinel, never equal to a real id
        for conversation_id, tool_name, trigger in rows:
            match = _PLANNER_STEP_TRIGGER.match(trigger or "")
            new_conversation = conversation_id != current_conversation_id
            if match is None or new_conversation:
                if current_plan:
                    plans.append(current_plan)
                    current_plan = []
                current_conversation_id = conversation_id
            if match is None:
                continue
            if int(match.group(1)) == 1 and current_plan:
                plans.append(current_plan)
                current_plan = []
            current_plan.append(tool_name)
        if current_plan:
            plans.append(current_plan)

        sequence_counts: dict[tuple[str, ...], int] = {}
        for plan in plans:
            if len(plan) < 2:
                continue
            key = tuple(plan)
            sequence_counts[key] = sequence_counts.get(key, 0) + 1

        return sorted(
            (
                {"actions": list(seq), "occurrences": count}
                for seq, count in sequence_counts.items()
                if count >= min_occurrences
            ),
            key=lambda e: e["occurrences"],
            reverse=True,
        )

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
            "verified": row.verified,
            "verification_detail": row.verification_detail,
            "created_at": row.created_at.isoformat(),
        }
