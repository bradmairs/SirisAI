"""Goal storage (milestone 23): goals and their objectives, persisted so
they survive past one conversation. Same "own short-lived session per
call" pattern as MemoryStore/EntityMemoryStore.

Deliberately just a store -- nothing here decides to work on a goal, check
it off, or notify about it. That's the planner (milestone 24) and later
proactive work; this milestone is only "SirisAI remembers what you're
trying to do."

`stale_goals()` (a later hardening pass, milestone 40) is "later proactive
work" arriving: a deterministic staleness check over what's already
stored, for `GoalCheckInSkill` (app/skills/builtin/goal_check_in.py) to
compose into the proactive check pipeline -- still not autonomous
judgment about whether a goal still matters, just "hasn't been touched in
a while."

Scoped per user (milestone 46, see app/core/user_context.py): every
method's `user_id` keyword defaults to the current request's resolved
user, and a goal_id/objective_id belonging to a different user is treated
as not found rather than forbidden -- the same "unknown id" response
either way, so a caller can't tell the difference between "doesn't exist"
and "exists but isn't yours."
"""

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.core.user_context import get_current_user_id
from app.db.models import Goal, GoalObjective


class GoalStore:
    def __init__(self, session_factory: sessionmaker) -> None:
        self._session_factory = session_factory

    def create_goal(
        self, title: str, objectives: list[str] | None = None, *, user_id: str | None = None
    ) -> dict[str, Any]:
        user_id = user_id or get_current_user_id()
        with self._session_factory() as session:
            goal = Goal(title=title, status="active", user_id=user_id)
            session.add(goal)
            session.flush()  # assigns goal.id without ending the transaction
            for description in objectives or []:
                session.add(GoalObjective(goal_id=goal.id, description=description))
            session.commit()
            return self._serialize_goal(session, goal.id)

    def list_goals(self, status: str | None = None, *, user_id: str | None = None) -> list[dict[str, Any]]:
        user_id = user_id or get_current_user_id()
        with self._session_factory() as session:
            stmt = (
                select(Goal.id)
                .where(Goal.user_id == user_id)
                .order_by(Goal.created_at.desc(), Goal.id.desc())
            )
            if status is not None:
                stmt = stmt.where(Goal.status == status)
            goal_ids = session.scalars(stmt).all()
            return [self._serialize_goal(session, goal_id) for goal_id in goal_ids]

    def update_goal_status(self, goal_id: int, status: str, *, user_id: str | None = None) -> dict[str, Any] | None:
        user_id = user_id or get_current_user_id()
        with self._session_factory() as session:
            goal = session.get(Goal, goal_id)
            if goal is None or goal.user_id != user_id:
                return None
            goal.status = status
            session.commit()
            return self._serialize_goal(session, goal_id)

    def add_objective(
        self, goal_id: int, description: str, *, user_id: str | None = None
    ) -> dict[str, Any] | None:
        user_id = user_id or get_current_user_id()
        with self._session_factory() as session:
            goal = session.get(Goal, goal_id)
            if goal is None or goal.user_id != user_id:
                return None
            session.add(GoalObjective(goal_id=goal_id, description=description))
            # Adding an objective is real progress on the goal, but it's a
            # GoalObjective row, not a write to the Goal row itself -- the
            # column's onupdate= only fires on an actual UPDATE to `goals`,
            # so this has to be set explicitly or stale_goals() (a
            # hardening pass, milestone 40) would see a goal getting
            # steady objective work as untouched for weeks.
            goal.updated_at = datetime.now(UTC)
            session.commit()
            return self._serialize_goal(session, goal_id)

    def complete_objective(self, objective_id: int, *, user_id: str | None = None) -> dict[str, Any] | None:
        """Marks one objective done and returns its parent goal (with the
        full updated objective list) -- there's rarely a use for the bare
        objective on its own once it's done."""
        user_id = user_id or get_current_user_id()
        with self._session_factory() as session:
            objective = session.get(GoalObjective, objective_id)
            if objective is None:
                return None
            goal_id = objective.goal_id
            goal = session.get(Goal, goal_id)
            if goal is None or goal.user_id != user_id:
                return None
            objective.done = True
            goal.updated_at = datetime.now(UTC)  # see add_objective()'s comment
            session.commit()
            return self._serialize_goal(session, goal_id)

    def stale_goals(
        self, days: int = 14, now: datetime | None = None, *, user_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Active goals not updated (status change, or an objective
        added/completed) in at least `days` days -- "still working on
        X?" material for the proactive check pipeline (a hardening pass,
        milestone 40). `now` is injectable for tests, same pattern as
        RoutineStore.due_routines(); comparisons are normalized to
        naive-UTC to match what SQLite hands back on read (see
        RoutineStore.due_routines()'s docstring for the full story)."""
        user_id = user_id or get_current_user_id()
        now = (now or datetime.now(UTC)).replace(tzinfo=None)
        cutoff = now - timedelta(days=days)
        with self._session_factory() as session:
            stmt = (
                select(Goal.id)
                .where(Goal.status == "active", Goal.updated_at <= cutoff, Goal.user_id == user_id)
                .order_by(Goal.updated_at)
            )
            goal_ids = session.scalars(stmt).all()
            return [self._serialize_goal(session, goal_id) for goal_id in goal_ids]

    def _serialize_goal(self, session: Any, goal_id: int) -> dict[str, Any]:
        goal = session.get(Goal, goal_id)
        objectives_stmt = select(GoalObjective).where(GoalObjective.goal_id == goal_id).order_by(GoalObjective.id)
        objectives = session.scalars(objectives_stmt).all()
        return {
            "id": goal.id,
            "title": goal.title,
            "status": goal.status,
            "created_at": goal.created_at.isoformat(),
            "updated_at": goal.updated_at.isoformat(),
            "objectives": [
                {"id": o.id, "description": o.description, "done": o.done} for o in objectives
            ],
        }
