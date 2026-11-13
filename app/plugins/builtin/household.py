"""Household tools (app/household/): the energy report and a camera summary;
and the audit digest (app/audit/digest.py)."""

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from app.household import cameras, energy
from app.plugins.base import Tool, ToolPermission

if TYPE_CHECKING:
    from app.core.siris import SirisAI


class EnergyReportTool(Tool):
    name = "energy_report"
    description = (
        "How much electricity the house used over the last N days (default 7) and what it cost, compared with the "
        "period before, the biggest users, and anything left on overnight. For 'how much power did we use this "
        "week', 'what's using the most electricity', 'is anything left on at night'."
    )
    permission = ToolPermission.READ
    parameters = {
        "type": "object",
        "properties": {"days": {"type": "integer", "description": "How many days (default 7, up to 30)."}},
        "required": [],
    }

    def __init__(self, siris: "SirisAI") -> None:
        self._siris = siris

    def run(self, days: int = 7, **kwargs: Any) -> Any:
        return energy.report(self._siris, max(1, min(int(days or 7), 30)))


class EnergyByDeviceTool(Tool):
    """6.58: what each device costs, and what's left on."""

    name = "energy_by_device"
    description = ("What each device costs to run: its kWh over the last N days (default 30) and a month's cost, what draws power "
                   "overnight, and what was on while everyone was out. For 'what does the pool pump cost', 'what's using power when "
                   "we're away', 'what's left on at night'.")
    permission = ToolPermission.READ
    parameters = {"type": "object", "properties": {"days": {"type": "integer", "description": "How many days (7-60, default 30)."}}, "required": []}

    def __init__(self, siris: "SirisAI") -> None:
        self._siris = siris

    def run(self, days: int = 30, **kwargs: Any) -> Any:
        from app.household import device_costs

        return device_costs.report(self._siris, max(7, min(int(days or 30), 60)))


class CameraSummaryTool(Tool):
    name = "camera_summary"
    description = (
        "A summary of what the cameras saw over the last N hours (default 12): people, cars, packages and couriers "
        "per camera, with times. For 'anything happen at the front door today', 'did a courier come'."
    )
    permission = ToolPermission.READ
    parameters = {
        "type": "object",
        "properties": {"hours": {"type": "number", "description": "How many hours back (default 12, up to 72)."}},
        "required": [],
    }

    def __init__(self, siris: "SirisAI") -> None:
        self._siris = siris

    def run(self, hours: float = 12, **kwargs: Any) -> Any:
        hours = max(0.5, min(float(hours or 12), 72))
        events = self._siris._frigate_client.events_since(datetime.now(UTC) - timedelta(hours=hours))  # type: ignore[union-attr]
        if not events:
            return {"hours": hours, "events": 0, "summary": "Nothing seen."}
        text, notes = cameras.summarise(self._siris, events)
        return {"hours": hours, "events": len(events), "summary": text, "descriptions": notes[:10]}


class AuditDigestTool(Tool):
    name = "audit_digest"
    description = (
        "What SirisAI did on its own over the last N days (default 7), what it held for the user's OK, what it was "
        "refused, wrong approval PINs, and settings changes. For 'what have you been doing this week', 'has anyone "
        "tried the PIN', 'did you do anything you shouldn't have'."
    )
    permission = ToolPermission.READ
    parameters = {
        "type": "object",
        "properties": {"days": {"type": "number", "description": "How many days back (default 7, up to 30)."}},
        "required": [],
    }

    def __init__(self, siris: "SirisAI") -> None:
        self._siris = siris

    def run(self, days: float = 7, **kwargs: Any) -> Any:
        from app.audit import digest

        return digest.digest(self._siris, max(1, min(float(days or 7), 30)))
