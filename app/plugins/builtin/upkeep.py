"""upkeep_status (app/upkeep/)."""

from typing import TYPE_CHECKING, Any

from app.plugins.base import Tool, ToolPermission
from app.upkeep import backups, housekeeping, models, updates

if TYPE_CHECKING:
    from app.core.siris import SirisAI


class UpkeepStatusTool(Tool):
    name = "upkeep_status"
    description = (
        "How SirisAI is looking after itself: its latest backups and the last restore drill, free disk space and "
        "database size, tokens used today, whether an update is available or was applied (or rolled back), and "
        "which model it's using. For 'are your backups ok', 'how much disk is left', 'are you up to date'."
    )
    permission = ToolPermission.READ
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, siris: "SirisAI") -> None:
        self._siris = siris

    def run(self, **kwargs: Any) -> Any:
        siris = self._siris
        history = siris.state.get(housekeeping.HISTORY_KEY) or []
        results = siris.state.get("daily_jobs_results") or {}
        status = updates.status(siris)
        return {
            "newest_backups": [{"name": b["name"], "at": b["at"], "size_mb": round(b["size_bytes"] / 1e6, 1)} for b in backups.list_backups(siris.settings)[:3]],
            "backup_warning": backups.freshness(siris),
            "last_restore_drill": (results.get("restore_drill") or {}).get("result"),
            "housekeeping": history[-1] if history else None,
            "version": status["running"],
            "last_update": status.get("last"),
            "update_waiting": status.get("pending"),
            "model": models.current_model(siris),
        }
