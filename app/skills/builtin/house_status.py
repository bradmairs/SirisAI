"""House status (milestone 73): answers "is everything okay at home?" --
the original brainstormed framing was "doors, windows, alarms, appliances
and unusual conditions," but "appliances" and "unusual conditions" have
no concrete, already-existing signal to check here (this codebase has no
appliance-power-draw sensor wired up anywhere, and "unusual" for a home
-- as opposed to a server -- isn't something any existing tool measures).
Scoped to what Home Assistant already exposes and this skill can check
honestly: doors/windows left open, locks left unlocked, and the alarm
panel's arm state.

Composes existing tools only, same shape as `ServerHealthSkill`/
`PresenceSkill` -- no new client, no new settings. "Door"/"window"/
"garage" detection is entity_id substring matching (binary_sensor's own
device_class isn't in `home_assistant_list_entities`'s trimmed
entity_id+state shape), the same heuristic style
`docker_resolve_container`/`home_assistant_resolve_entity` (milestone 66)
already use rather than inventing a stricter classification scheme this
milestone doesn't need. A binary_sensor's "on" state means the condition
is detected (Home Assistant's own convention) -- open, not closed.
"""

from typing import TYPE_CHECKING, Any

from app.skills.base import Skill

if TYPE_CHECKING:
    from app.core.siris import SirisAI

_OPENING_KEYWORDS = ("door", "window", "garage")


class HouseStatusSkill(Skill):
    name = "house_status_check"
    description = (
        "Answer 'is everything okay at home?' -- checks Home Assistant for doors/windows left "
        "open, locks left unlocked, and the alarm panel's arm state. Read-only."
    )
    parameters = {"type": "object", "properties": {}, "required": []}

    def run(self, siris: "SirisAI", **kwargs: Any) -> Any:
        if siris.tools.get("home_assistant_list_entities") is None:
            return {"checked": False, "reason": "Home Assistant is not configured."}

        binary_sensors = siris.run_tool(
            "home_assistant_list_entities", {"domain": "binary_sensor"}
        )
        open_openings = [
            s["entity_id"]
            for s in binary_sensors
            if s["state"] == "on"
            and any(keyword in s["entity_id"].lower() for keyword in _OPENING_KEYWORDS)
        ]

        locks = siris.run_tool("home_assistant_list_entities", {"domain": "lock"})
        unlocked_locks = [
            entity["entity_id"] for entity in locks if entity["state"] == "unlocked"
        ]

        alarm_panels = siris.run_tool(
            "home_assistant_list_entities", {"domain": "alarm_control_panel"}
        )

        warnings: list[str] = []
        if open_openings:
            warnings.append(f"Open: {', '.join(open_openings)}")
        if unlocked_locks:
            warnings.append(f"Unlocked: {', '.join(unlocked_locks)}")

        return {
            "checked": True,
            "open_openings": open_openings,
            "unlocked_locks": unlocked_locks,
            "alarm_panels": alarm_panels,
            "warnings": warnings,
            "all_secure": not warnings,
        }
