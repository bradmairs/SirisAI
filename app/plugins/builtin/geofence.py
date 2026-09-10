"""Geofence reminder tools -- how the LLM creates and manages reminders
triggered by a Home Assistant entity's zone (app/geofence/service.py,
app/db/models.py's GeofenceReminder). Always registered, same as the
goal tools: this is core state, not an optional integration -- the
underlying check (GeofenceReminderCheckSkill) is what actually needs
Home Assistant configured, same "store exists regardless, the check
that reads live state doesn't" split app/skills/builtin/geofence_reminder.py
draws.

Three tools rather than a remember/recall pair, same reasoning as
goals.py: a reminder has a lifecycle (created, later disabled/re-enabled,
eventually fires) rather than just being written and read back.
"""

from typing import Any

from app.geofence.service import GeofenceReminderStore
from app.plugins.base import Tool, ToolPermission


class CreateGeofenceReminderTool(Tool):
    name = "create_geofence_reminder"
    description = (
        "Create a reminder that fires when a Home Assistant person/device_tracker entity's "
        "zone changes to a specific value, e.g. entity_id='person.brad', zone='home', "
        "message='pick up the mail'. Use home_assistant_resolve_entity or "
        "home_assistant_list_entities (domain='person' or 'device_tracker') to find the "
        "entity_id, and home_assistant_get_state on it to see its current zone-shaped state "
        "(e.g. 'home', 'not_home', or a configured zone's name). By default fires once then "
        "disables itself, like a normal reminder; set repeat=true for a standing rule that "
        "keeps firing on every future transition into that zone instead."
    )
    permission = ToolPermission.SAFE_ACTION
    parameters = {
        "type": "object",
        "properties": {
            "entity_id": {
                "type": "string",
                "description": "The Home Assistant person/device_tracker entity to watch.",
            },
            "zone": {
                "type": "string",
                "description": "The zone state to watch for, e.g. 'home', 'not_home', or a configured zone's name.",
            },
            "message": {
                "type": "string",
                "description": "What to remind about when this fires.",
            },
            "repeat": {
                "type": "boolean",
                "description": "Keep firing on every future transition into this zone, instead of once. Defaults to false.",
            },
        },
        "required": ["entity_id", "zone", "message"],
    }

    def __init__(self, geofence: GeofenceReminderStore) -> None:
        self._geofence = geofence

    def run(
        self,
        entity_id: str = "",
        zone: str = "",
        message: str = "",
        repeat: bool = False,
        **kwargs: Any,
    ) -> Any:
        return self._geofence.create(entity_id, zone, message, repeat=repeat)


class ListGeofenceRemindersTool(Tool):
    name = "list_geofence_reminders"
    description = "List geofence reminders, optionally only the enabled ones."
    permission = ToolPermission.READ
    parameters = {
        "type": "object",
        "properties": {
            "enabled_only": {
                "type": "boolean",
                "description": "Only return currently-enabled reminders. Defaults to false.",
            },
        },
        "required": [],
    }

    def __init__(self, geofence: GeofenceReminderStore) -> None:
        self._geofence = geofence

    def run(self, enabled_only: bool = False, **kwargs: Any) -> Any:
        return self._geofence.list_all(enabled_only=enabled_only)


class SetGeofenceReminderEnabledTool(Tool):
    name = "set_geofence_reminder_enabled"
    description = (
        "Enable or disable a geofence reminder, using its id from list_geofence_reminders -- "
        "disabling is how to cancel one without deleting its history."
    )
    permission = ToolPermission.SAFE_ACTION
    parameters = {
        "type": "object",
        "properties": {
            "reminder_id": {"type": "integer", "description": "The reminder's id."},
            "enabled": {"type": "boolean", "description": "Whether it should be enabled."},
        },
        "required": ["reminder_id", "enabled"],
    }

    def __init__(self, geofence: GeofenceReminderStore) -> None:
        self._geofence = geofence

    def run(self, reminder_id: int = 0, enabled: bool = True, **kwargs: Any) -> Any:
        return self._geofence.set_enabled(reminder_id, enabled)
