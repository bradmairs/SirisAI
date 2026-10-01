"""Energy tools (app/energy/service.py)."""

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from app.energy import service
from app.plugins.base import Tool, ToolPermission

if TYPE_CHECKING:
    from app.core.siris import SirisAI

_NOT_DEFERRABLE = {"schedule_for_cheap_energy", "cancel_deferred_job", "run_protocol", "undo_last_protocol"}


class EnergyStatusTool(Tool):
    name = "energy_status"
    description = (
        "Whether electricity is cheap right now (tariff window, solar output, or live price) and when the next cheap "
        "window starts, plus any jobs waiting for cheap power."
    )
    permission = ToolPermission.READ
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, siris: "SirisAI") -> None:
        self._siris = siris

    def run(self, **kwargs: Any) -> Any:
        return {**service.status(self._siris), "waiting_jobs": self._siris.deferred_jobs.list("waiting")}


class ScheduleForCheapEnergyTool(Tool):
    name = "schedule_for_cheap_energy"
    description = (
        "Run a tool call later, when power is cheap (cheap tariff window, plenty of solar, or a low price) -- e.g. "
        "start the dishwasher, a backup, or a big download then instead of now. Optionally give a deadline in hours "
        "after which it runs anyway."
    )
    permission = ToolPermission.CONFIRM_REQUIRED
    parameters = {
        "type": "object",
        "properties": {
            "tool_name": {"type": "string", "description": "The tool to run later."},
            "arguments": {"type": "object", "description": "Its arguments."},
            "reason": {"type": "string", "description": "Short label, e.g. 'dishwasher'."},
            "deadline_hours": {"type": "number", "description": "Run anyway after this many hours."},
        },
        "required": ["tool_name"],
    }

    def __init__(self, siris: "SirisAI") -> None:
        self._siris = siris

    def _target_needs_confirmation(self, arguments: dict[str, Any]) -> bool:
        target = self._siris.tools.get(arguments.get("tool_name", ""))
        if target is None:
            return False
        if target.permission is ToolPermission.RESTRICTED:
            return True
        return target.permission is ToolPermission.CONFIRM_REQUIRED and target.needs_confirmation(arguments.get("arguments") or {})

    def needs_confirmation(self, arguments: dict[str, Any]) -> bool:
        """Exactly as much approval as the deferred call itself would need."""
        return self._target_needs_confirmation(arguments)

    def run(self, tool_name: str = "", arguments: dict[str, Any] | None = None, reason: str = "", deadline_hours: float | None = None, **kwargs: Any) -> Any:
        target = self._siris.tools.get(tool_name)
        if target is None:
            raise ValueError(f"Unknown tool: {tool_name}")
        if tool_name in _NOT_DEFERRABLE:
            raise ValueError(f"{tool_name} can't be deferred.")
        if not service.status(self._siris)["configured"]:
            raise ValueError("No cheap-power signal is configured (SIRISAI_ENERGY_CHEAP_WINDOWS / _SOLAR_ENTITY_ID / _PRICE_ENTITY_ID).")
        call = {"tool_name": tool_name, "arguments": arguments or {}}
        # Reaching run() means any confirmation the target needs was given.
        confirmed = self._target_needs_confirmation(call)
        deadline = datetime.now(UTC) + timedelta(hours=deadline_hours) if deadline_hours else None
        job = self._siris.deferred_jobs.add(tool_name, arguments or {}, reason, confirmed, deadline)
        return {**job, "energy": service.status(self._siris)}


class ListDeferredJobsTool(Tool):
    name = "list_deferred_jobs"
    description = "Jobs waiting for cheap power, and recently run ones."
    permission = ToolPermission.READ
    parameters = {"type": "object", "properties": {"include_finished": {"type": "boolean"}}, "required": []}

    def __init__(self, siris: "SirisAI") -> None:
        self._siris = siris

    def run(self, include_finished: bool = False, **kwargs: Any) -> Any:
        return self._siris.deferred_jobs.list(None if include_finished else "waiting")


class CancelDeferredJobTool(Tool):
    name = "cancel_deferred_job"
    description = "Cancel a job that is waiting for cheap power (id from list_deferred_jobs)."
    permission = ToolPermission.SAFE_ACTION
    parameters = {"type": "object", "properties": {"job_id": {"type": "integer"}}, "required": ["job_id"]}

    def __init__(self, siris: "SirisAI") -> None:
        self._siris = siris

    def run(self, job_id: int = 0, **kwargs: Any) -> Any:
        return {"job_id": job_id, "cancelled": self._siris.deferred_jobs.cancel(job_id)}
