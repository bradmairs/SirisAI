"""Goal decomposition check (a Jarvis-concept brainstorm item, chief-of-
staff category): flags active goals that are still just a title, with
no objectives recorded yet -- "I want to learn Spanish" with no concrete
next step underneath it.

Deliberately not an LLM call that invents objectives on the model's
behalf: this codebase's own `create_goal`/`add_objective` tools already
let the model (or user) decompose a goal into steps whenever it likes --
this skill's only job is the deterministic part, noticing a goal that
never got that treatment, the same "flag it, don't fix it" boundary
`GoalCheckInSkill`'s staleness check and `ServerHealthSkill`'s threshold
checks already draw. Composed into `POST /siris/proactive/check`
alongside `goal_check_in`, feeding the same `NotificationManager` pass.

Distinct from staleness: a brand-new goal with no objectives shows up
here on its very first check, long before `stale_goals()`'s
not-touched-in-14-days threshold would ever catch it.
"""

from typing import TYPE_CHECKING, Any

from app.skills.base import Skill

if TYPE_CHECKING:
    from app.core.siris import SirisAI


class GoalDecompositionCheckSkill(Skill):
    name = "goal_decomposition_check"
    description = (
        "Check for active goals that have no objectives yet -- just a title, no concrete next "
        "step recorded. A deterministic check (objective count == 0), not a judgment about "
        "whether the goal is a good one."
    )
    parameters = {"type": "object", "properties": {}, "required": []}

    def run(self, siris: "SirisAI", **kwargs: Any) -> Any:
        needing = siris.goals.goals_needing_decomposition()
        warnings = [
            f"Goal '{g['title']}' has no objectives yet -- break it down into concrete next steps."
            for g in needing
        ]
        return {"goals_needing_decomposition": needing, "warnings": warnings, "healthy": not warnings}
