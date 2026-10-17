"""career_log_cpd and career_drafts (app/career/capture.py)."""

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from app.career import capture
from app.core import clock
from app.plugins.base import Tool, ToolPermission

if TYPE_CHECKING:
    from app.core.siris import SirisAI


class CareerLogCpdTool(Tool):
    name = "career_log_cpd"
    description = (
        "Add an activity to Brad's CPD list (a draft to copy into Engineers Australia, where CPD is actually "
        "logged). For 'log 2 hours of CPD for the drainage webinar', 'I did a 1 hour safety course, count it'."
    )
    permission = ToolPermission.SAFE_ACTION
    parameters = {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "What the activity was."},
            "hours": {"type": "number", "description": "How long, in hours."},
            "date": {"type": "string", "description": "YYYY-MM-DD. Defaults to today."},
            "category": {"type": "string", "enum": ["Area of practice", "Risk management", "Business and management"], "description": "Defaults to a guess from the title."},
            "provider": {"type": "string", "description": "Optional: who ran it."},
        },
        "required": ["title", "hours"],
    }

    def __init__(self, siris: "SirisAI") -> None:
        self._siris = siris

    def run(self, title: str = "", hours: float = 1.0, date: str | None = None, category: str | None = None, provider: str = "", **kwargs: Any) -> Any:
        if not title.strip() or float(hours) <= 0:
            raise ValueError("Give the activity and a number of hours above zero.")
        day = date or datetime.now(UTC).astimezone(clock.get_zone(self._siris.settings.timezone)).date().isoformat()
        return capture.add_cpd(self._siris, title, float(hours), day, category, provider)


class CareerDraftsTool(Tool):
    name = "career_drafts"
    description = (
        "The CPD activities and chartership evidence SirisAI has collected as drafts (from the calendar, from "
        "engineering work, or logged in chat), with total CPD hours. For 'what CPD have I logged', 'what evidence "
        "have I got for chartership'."
    )
    permission = ToolPermission.READ
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, siris: "SirisAI") -> None:
        self._siris = siris

    def run(self, **kwargs: Any) -> Any:
        found = capture.drafts(self._siris)
        return {**found, "cpd_hours": round(sum(e["hours"] for e in found["cpd"]), 2)}
