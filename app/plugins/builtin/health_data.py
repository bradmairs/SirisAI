"""health_summary (app/health/service.py)."""

from typing import TYPE_CHECKING, Any

from app.core import clock
from app.health.service import summary
from app.plugins.base import Tool, ToolPermission

if TYPE_CHECKING:
    from app.core.siris import SirisAI


class HealthSummaryTool(Tool):
    name = "health_summary"
    description = (
        "The user's recent health data from their phone -- sleep, steps, resting heart rate, HRV, activity, weight -- "
        "with the latest day compared to their own usual. For 'how did I sleep', 'am I moving enough this week'."
    )
    permission = ToolPermission.READ
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, siris: "SirisAI") -> None:
        self._siris = siris

    def run(self, **kwargs: Any) -> Any:
        return summary(self._siris.health, clock.now_local(self._siris.settings.timezone).date())
