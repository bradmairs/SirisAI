"""Goal tools -- how the LLM creates and updates goals (milestone 23).
Always registered, same as the memory tools: goals are core state, not an
optional integration.

Five tools rather than a remember/recall pair, because a goal has more of
a lifecycle than a fact does: it's created with objectives, objectives get
added or completed over time, and the goal itself gets closed out. Kept as
separate single-purpose tools (create_goal, list_goals,
update_goal_status, add_goal_objective, complete_goal_objective) rather
than one do-everything tool with a pile of optional arguments, same
single-purpose-tool convention as the rest of app/plugins/builtin/.
"""

from typing import Any

from app.goals.service import GoalStore
from app.plugins.base import Tool, ToolPermission


class CreateGoalTool(Tool):
    name = "create_goal"
    description = (
        "Create a goal that persists across conversations, e.g. title='Reduce homelab power "
        "consumption'. Optionally give it an initial list of objectives (concrete steps toward "
        "it) -- more can be added later with add_goal_objective. Starts in 'active' status."
    )
    permission = ToolPermission.SAFE_ACTION
    parameters = {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "What the goal is, in a sentence or so."},
            "objectives": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional initial list of concrete steps toward this goal.",
            },
        },
        "required": ["title"],
    }

    def __init__(self, goals: GoalStore) -> None:
        self._goals = goals

    def run(self, title: str = "", objectives: list[str] | None = None, **kwargs: Any) -> Any:
        return self._goals.create_goal(title, objectives=objectives)


class ListGoalsTool(Tool):
    name = "list_goals"
    description = (
        "List goals and their objectives, most recently created first. Omit status to get all "
        "goals; pass status='active' (the default a new goal starts in) to see only ones still "
        "being worked on."
    )
    permission = ToolPermission.READ
    parameters = {
        "type": "object",
        "properties": {
            "status": {"type": "string", "description": "Optional exact-match filter, e.g. 'active'."},
        },
        "required": [],
    }

    def __init__(self, goals: GoalStore) -> None:
        self._goals = goals

    def run(self, status: str | None = None, **kwargs: Any) -> Any:
        return self._goals.list_goals(status=status)


class UpdateGoalStatusTool(Tool):
    name = "update_goal_status"
    description = (
        "Change a goal's overall status, e.g. status='completed' once it's done or "
        "status='abandoned' if it's no longer being pursued. Use the goal_id from create_goal "
        "or list_goals."
    )
    permission = ToolPermission.SAFE_ACTION
    parameters = {
        "type": "object",
        "properties": {
            "goal_id": {"type": "integer", "description": "The goal to update."},
            "status": {"type": "string", "description": "New status, e.g. 'completed' or 'abandoned'."},
        },
        "required": ["goal_id", "status"],
    }

    def __init__(self, goals: GoalStore) -> None:
        self._goals = goals

    def run(self, goal_id: int = 0, status: str = "", **kwargs: Any) -> Any:
        result = self._goals.update_goal_status(goal_id, status)
        if result is None:
            raise ValueError(f"Unknown goal_id: {goal_id}")
        return result


class AddGoalObjectiveTool(Tool):
    name = "add_goal_objective"
    description = "Add a new objective (concrete step) to an existing goal."
    permission = ToolPermission.SAFE_ACTION
    parameters = {
        "type": "object",
        "properties": {
            "goal_id": {"type": "integer", "description": "The goal to add an objective to."},
            "description": {"type": "string", "description": "The new objective."},
        },
        "required": ["goal_id", "description"],
    }

    def __init__(self, goals: GoalStore) -> None:
        self._goals = goals

    def run(self, goal_id: int = 0, description: str = "", **kwargs: Any) -> Any:
        result = self._goals.add_objective(goal_id, description)
        if result is None:
            raise ValueError(f"Unknown goal_id: {goal_id}")
        return result


class CompleteGoalObjectiveTool(Tool):
    name = "complete_goal_objective"
    description = (
        "Mark one objective done, using the objective_id from create_goal/list_goals' "
        "objectives list. Returns the parent goal with its updated objective list -- marking "
        "the last objective done does not automatically complete the goal itself, use "
        "update_goal_status for that."
    )
    permission = ToolPermission.SAFE_ACTION
    parameters = {
        "type": "object",
        "properties": {
            "objective_id": {"type": "integer", "description": "The objective to mark done."},
        },
        "required": ["objective_id"],
    }

    def __init__(self, goals: GoalStore) -> None:
        self._goals = goals

    def run(self, objective_id: int = 0, **kwargs: Any) -> Any:
        result = self._goals.complete_objective(objective_id)
        if result is None:
            raise ValueError(f"Unknown objective_id: {objective_id}")
        return result
