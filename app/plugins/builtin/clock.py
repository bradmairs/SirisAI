"""Current date and time. The model has no clock of its own (asked the date it
answered "October 27, 2023"); the context block already carries it, this
lets a plan ask explicitly, e.g. to work out "in 20 minutes"."""

from typing import Any

from app.core.clock import now_local
from app.plugins.base import Tool, ToolPermission


class GetCurrentTimeTool(Tool):
    name = "get_current_time"
    description = "Get the current date, time, weekday and timezone. Use it for anything that depends on 'now' (today, tomorrow, in 20 minutes, tonight)."
    permission = ToolPermission.READ
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, timezone: str) -> None:
        self._timezone = timezone

    def run(self, **kwargs: Any) -> Any:
        now = now_local(self._timezone)
        return {
            "iso": now.isoformat(timespec="seconds"),
            "date": now.strftime("%Y-%m-%d"),
            "time": now.strftime("%H:%M"),
            "weekday": now.strftime("%A"),
            "timezone": self._timezone,
        }


class BriefMeTool(Tool):
    """4.31.0: the short spoken brief, kept ready (app/voice/brief_cache.py)."""

    name = "brief_me"
    description = (
        "A short spoken brief for right now: the weather, what's on today, readiness and strain, news and how "
        "many things need you. Ready in advance, so it answers at once. For the full briefing (goals, Plex, "
        "email), use daily_briefing."
    )
    permission = ToolPermission.READ
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, siris: Any) -> None:
        self._siris = siris

    def run(self, **kwargs: Any) -> Any:
        from app.voice import brief_cache

        return {"message": brief_cache.get(self._siris)}
