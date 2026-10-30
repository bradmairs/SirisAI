"""Unrecognized network device alert (a hardening pass): flags a Home
Assistant device_tracker entity SirisAI hasn't seen before, composed
into the same POST /siris/proactive/check pipeline as
server_health_check/event_timing_check/goal_check_in.

Deliberately built on Home Assistant's device_tracker entities, not
UniFi directly. UniFiClient.list_clients() (app/unifi/client.py)
currently always raises: a real controller was live-tested and its API
key only authenticates against UniFi's cloud Site Manager API, which has
no client-listing endpoint at all -- only a local controller's separate
Integration API exposes that. device_tracker is the one already-working
path to "what's currently on the network" this codebase has (whether
populated by a UniFi HACS integration, ping, or anything else Home
Assistant tracks devices with) -- if that domain has no entities,
`checked` comes back false rather than pretending there's nothing to
report.

The known-device set (KnownDeviceStore, app/network_devices/service.py)
persists across restarts on purpose -- an in-memory set would forget
every known device on every process restart and flag the whole household
as "new" the next time it runs. The very first check ever run has
nothing to compare against, so (same "baseline, not a guess" pattern as
PresenceSkill, milestone 42) it just records what's there rather than
reporting every device as newly arrived.
"""

from typing import TYPE_CHECKING, Any

from app.skills.base import Skill

if TYPE_CHECKING:
    from app.core.siris import SirisAI


class NewDeviceAlertSkill(Skill):
    name = "new_device_alert"
    description = (
        "Check for Home Assistant device_tracker entities (network devices) that haven't been "
        "seen before, and warn about any found. Read-only -- doesn't send anything on its own; "
        "POST /siris/proactive/check does that, this just detects."
    )
    parameters = {"type": "object", "properties": {}, "required": []}

    def run(self, siris: "SirisAI", **kwargs: Any) -> Any:
        if siris.tools.get("home_assistant_list_entities") is None:
            return {"checked": False, "new_devices": [], "warnings": []}
        from app.homelab import devices

        if devices.enabled(siris):
            # UniFi's client list is watched instead (app/homelab/devices.py),
            # with Name it / Block in the feed; don't alert twice.
            return {"checked": False, "new_devices": [], "warnings": [], "note": "UniFi's device watch is on"}

        entities = siris.run_tool("home_assistant_list_entities", {"domain": "device_tracker"})
        current_ids = {e["entity_id"] for e in entities}
        known_ids = siris.known_devices.list_known()

        if not known_ids:
            # First-ever check (or nothing's ever been marked known):
            # establish the baseline instead of flagging every current
            # device as "new".
            siris.known_devices.mark_known(current_ids)
            return {"checked": True, "new_devices": [], "warnings": []}

        new_ids = sorted(current_ids - known_ids)
        if new_ids:
            siris.known_devices.mark_known(new_ids)

        return {
            "checked": True,
            "new_devices": new_ids,
            "warnings": [f"Unrecognized device connected: {entity_id}" for entity_id in new_ids],
        }
