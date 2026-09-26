"""Reminder and timer tools: set, list, cancel. Delivery is the background
scheduler's job (app/core/scheduler.py), which pushes a notification when a
reminder comes due."""

from datetime import UTC, datetime, timedelta
from typing import Any

from app.core.clock import to_local
from app.plugins.base import Tool, ToolPermission
from app.reminders.service import ReminderStore, parse_when


class SetReminderTool(Tool):
    name = "set_reminder"
    description = (
        "Set a reminder or timer that sends the user a push notification when it comes due. "
        "For 'set a timer for 10 minutes' or 'remind me in an hour' pass in_minutes. For 'remind me at "
        "7pm' or 'tomorrow at 07:30' pass at (a clock time like '19:00' or '7:30pm', or a date-time like "
        "'2026-09-27T07:30' in the user's timezone -- work the date out from the current date/time you were given). "
        "message is what the notification should say."
    )
    permission = ToolPermission.SAFE_ACTION
    parameters = {
        "type": "object",
        "properties": {
            "message": {"type": "string", "description": "What to remind about, e.g. 'take the bins out'. For a plain timer, 'Timer finished'."},
            "in_minutes": {"type": "number", "description": "Fire this many minutes from now. Use this or `at`, not both."},
            "at": {"type": "string", "description": "When to fire: '19:00', '7:30pm' (next occurrence) or an ISO date-time."},
        },
        "required": ["message"],
    }

    def __init__(self, store: ReminderStore, timezone: str) -> None:
        self._store = store
        self._timezone = timezone

    def run(self, message: str = "", in_minutes: float | None = None, at: str | None = None, **kwargs: Any) -> Any:
        if (in_minutes is None) == (not at):
            raise ValueError("Give exactly one of in_minutes or at.")
        now = datetime.now(UTC)
        if in_minutes is not None:
            if not 0.1 <= float(in_minutes) <= 60 * 24 * 365:
                raise ValueError("in_minutes must be between 0.1 and a year's worth of minutes.")
            due = now + timedelta(minutes=float(in_minutes))
        else:
            due = parse_when(at or "", self._timezone, now)
            if due <= now:
                raise ValueError("That time has already passed. Give a future time.")
        reminder = self._store.create(message or "Reminder", due)
        return {**reminder, "due_local": to_local(reminder["due_at"], self._timezone)}


class ListRemindersTool(Tool):
    name = "list_reminders"
    description = "List pending reminders and timers, soonest first."
    permission = ToolPermission.READ
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, store: ReminderStore, timezone: str) -> None:
        self._store = store
        self._timezone = timezone

    def run(self, **kwargs: Any) -> Any:
        return [{**r, "due_local": to_local(r["due_at"], self._timezone)} for r in self._store.list_pending()]


class CancelReminderTool(Tool):
    name = "cancel_reminder"
    description = "Cancel a pending reminder or timer by its id (from list_reminders)."
    permission = ToolPermission.SAFE_ACTION
    parameters = {
        "type": "object",
        "properties": {"reminder_id": {"type": "integer", "description": "The reminder's id, from list_reminders."}},
        "required": ["reminder_id"],
    }

    def __init__(self, store: ReminderStore) -> None:
        self._store = store

    def run(self, reminder_id: int = 0, **kwargs: Any) -> Any:
        if not self._store.cancel(reminder_id):
            raise ValueError(f"No pending reminder with id {reminder_id}.")
        return {"cancelled": True, "id": reminder_id}
