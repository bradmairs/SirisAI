"""what_have_you_learned (app/learning/)."""

from typing import TYPE_CHECKING, Any

from app.learning import feedback
from app.learning.reflection import HISTORY_KEY, learned_cases
from app.plugins.base import Tool, ToolPermission

if TYPE_CHECKING:
    from app.core.siris import SirisAI


class WhatHaveYouLearnedTool(Tool):
    name = "what_have_you_learned"
    description = (
        "What SirisAI has learned from how things went: what it now asks before doing on its own (because you undid "
        "it), which suggestions you accept or turn down, corrections that became eval cases, and last night's "
        "reflection. For 'what have you learned', 'why did you ask me first', 'how are your suggestions going'."
    )
    permission = ToolPermission.READ
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, siris: "SirisAI") -> None:
        self._siris = siris

    def run(self, **kwargs: Any) -> Any:
        history = self._siris.state.get(HISTORY_KEY) or []
        return {
            "asking_first_now": feedback.demoted_keys(self._siris),
            "how_things_went_30_days": self._siris.outcomes.summary(days=30)[:15],
            "learned_eval_cases": len(learned_cases(self._siris)),
            "last_reflection": history[-1] if history else None,
        }
