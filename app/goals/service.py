"""Goal storage (milestone 23): goals and their objectives, persisted so
they survive past one conversation. Same "own short-lived session per
call" pattern as MemoryStore/EntityMemoryStore.

Deliberately just a store -- nothing here decides to work on a goal, check
it off, or notify about it. That's the planner (milestone 24) and later
proactive work; this milestone is only "SirisAI remembers what you're
trying to do."
"""

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.db.models import Goal, GoalObjective


class GoalStore:
    def __init__(self, session_factory: sessionmaker) -> None:
        self._session_factory = session_factory

    def create_goal(self, title: str, objectives: list[str] | None = None) -> dict[str, Any]:
        with self._session_factory() as session:
            goal = Goal(title=title, status="active")
            session.add(goal)
            session.flush()  # assigns goal.id without ending the transaction
            for description in objectives or []:
                session.add(GoalObjective(goal_id=goal.id, description=description))
            session.commit()
            return self._serialize_goal(session, goal.id)

    def list_goals(self, status: str | None = None) -> list[dict[str, Any]]:
        with self._session_factory() as session:
            stmt = select(Goal.id).order_by(Goal.created_at.desc(), Goal.id.desc())
            if status is not None:
                stmt = stmt.where(Goal.status == status)
            goal_ids = session.scalars(stmt).all()
            return [self._serialize_goal(session, goal_id) for goal_id in goal_ids]

    def update_goal_status(self, goal_id: int, status: str) -> dict[str, Any] | None:
        with self._session_factory() as session:
            goal = session.get(Goal, goal_id)
            if goal is None:
                return None
            goal.status = status
            session.commit()
            return self._serialize_goal(session, goal_id)

    def add_objective(self, goal_id: int, description: str) -> dict[str, Any] | None:
        with self._session_factory() as session:
            goal = session.get(Goal, goal_id)
            if goal is None:
                return None
            session.add(GoalObjective(goal_id=goal_id, description=description))
            session.commit()
            return self._serialize_goal(session, goal_id)

    def complete_objective(self, objective_id: int) -> dict[str, Any] | None:
        """Marks one objective done and returns its parent goal (with the
        full updated objective list) -- there's rarely a use for the bare
        objective on its own once it's done."""
        with self._session_factory() as session:
            objective = session.get(GoalObjective, objective_id)
            if objective is None:
                return None
            objective.done = True
            goal_id = objective.goal_id
            session.commit()
            return self._serialize_goal(session, goal_id)

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
