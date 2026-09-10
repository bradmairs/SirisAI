"""Geofence reminder check (a Jarvis-concept brainstorm item): fires each
enabled GeofenceReminder (app/geofence/service.py) whose watched Home
Assistant entity just transitioned into the target zone.

Same "trigger condition -> action" shape PresenceSkill (milestone 42)
established, generalized from one hardcoded household home/away pair to
an arbitrary entity/zone/message set by the user. Also same
transition-only discipline: a reminder only fires on the tick its
entity's zone actually *changes into* the watched one, not on every
check while it happens to already be there -- otherwise a reminder for
"person.brad" + zone="home" would refire every few minutes for as long
as Brad stayed home. The very first check for a given reminder has no
prior zone to compare against, so it just records a baseline rather than
assuming a transition just happened.

Delivered via the `notify` tool directly, not through NotificationManager
(app/notify/manager.py): a reminder's message is arbitrary text the user
already wrote and approved by creating the reminder, not an
auto-generated warning that needs severity/action classification --
same directness PresenceSkill already uses for its own scene activation.

One entity failing to look up (a stale/deleted Home Assistant entity_id)
is skipped rather than aborting the whole check -- one broken reminder
shouldn't stop every other one's check from happening.
"""

from typing import TYPE_CHECKING, Any

from app.skills.base import Skill

if TYPE_CHECKING:
    from app.core.siris import SirisAI


class GeofenceReminderCheckSkill(Skill):
    name = "geofence_reminder_check"
    description = (
        "Check every enabled geofence reminder's entity for a transition into its watched "
        "zone, and send a notification (via notify, if configured) for any that just fired. "
        "Composed into POST /siris/proactive/check. Read-only in the sense that it doesn't "
        "create/modify reminders -- create_geofence_reminder does that -- but does send "
        "notifications and update each reminder's recorded zone as a side effect."
    )
    parameters = {"type": "object", "properties": {}, "required": []}

    def run(self, siris: "SirisAI", **kwargs: Any) -> Any:
        if siris.tools.get("home_assistant_get_state") is None:
            return {"checked": False, "fired": []}

        fired: list[dict[str, Any]] = []
        for reminder in siris.geofence.list_all(enabled_only=True):
            try:
                state = siris.run_tool(
                    "home_assistant_get_state", {"entity_id": reminder["entity_id"]}
                )
            except Exception:
                continue
            current_zone = state.get("state")
            previous_zone = reminder["last_known_zone"]

            if previous_zone is None:
                siris.geofence.record_zone(reminder["id"], current_zone)
                continue

            entered_zone = current_zone == reminder["zone"] and previous_zone != reminder["zone"]
            if not entered_zone:
                if current_zone != previous_zone:
                    siris.geofence.record_zone(reminder["id"], current_zone)
                continue

            if siris.tools.get("notify") is not None:
                siris.run_tool(
                    "notify",
                    {"title": "SirisAI: reminder", "message": reminder["message"]},
                    trigger=(
                        f"geofence_reminder_check: {reminder['entity_id']} entered "
                        f"zone '{reminder['zone']}'"
                    ),
                )
            siris.geofence.record_zone(
                reminder["id"], current_zone, disable=not reminder["repeat"]
            )
            fired.append({"id": reminder["id"], "message": reminder["message"]})

        return {"checked": True, "fired": fired}
