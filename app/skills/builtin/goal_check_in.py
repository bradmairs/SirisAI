"""Goal check-ins (milestone 40): a deterministic staleness check over
active goals -- "still working on X?" instead of a goal only ever being
looked at again when explicitly asked. `GoalStore.stale_goals()`
(app/goals/service.py) does the actual query; this is the thin skill
wrapper, same shape as `ServerHealthSkill`/`EventTimingSkill` -- a
deterministic threshold, no fuzzy LLM judgment about whether the goal
still matters, so it composes into `POST /siris/proactive/check` the
same way.
"""

from datetime import datetime
from typing import TYPE_CHECKING, Any

from app.skills.base import Skill

if TYPE_CHECKING:
    from app.core.siris import SirisAI

_STALE_AFTER_DAYS = 14


class GoalCheckInSkill(Skill):
    name = "goal_check_in"
    description = (
        "Check whether any active goal hasn't been touched -- no status change, no objective "
        "added or completed -- in a while. A deterministic staleness check, not a judgment "
        "about whether the goal still matters."
    )
    parameters = {"type": "object", "properties": {}, "required": []}

    def run(self, siris: "SirisAI", now: datetime | None = None, **kwargs: Any) -> Any:
        stale = siris.goals.stale_goals(days=_STALE_AFTER_DAYS, now=now)
        warnings = [
            f"Goal '{g['title']}' hasn't been updated in over {_STALE_AFTER_DAYS} days (still active)."
            for g in stale
        ]
        return {"stale_goals": stale, "warnings": warnings, "healthy": not warnings}
