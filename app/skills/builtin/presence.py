"""Presence-triggered scenes (milestone 42): "everyone left" -> away
scene, "someone's home" -> home/welcome scene. UniFi presence (who's
connected to the network) composed with a Home Assistant scene call --
a new lightweight "trigger condition -> action" concept is the one
genuinely new piece here; everything it reads from
(`unifi_list_clients`) and acts through (`home_assistant_call_service`)
already exists.

Deliberately not a general rules engine: exactly two conditions (arrived,
left) and exactly one action each (activate a pre-configured HA scene),
not an arbitrary trigger/action DSL this milestone doesn't need.

Transition-only by design -- calling the away scene on every single
5-minute cron tick while everyone's still away would be noise, not a
feature. "Currently home" is compared against the last recorded
presence state (an Event, same "reuse EventMemoryStore rather than a new
store" reasoning as milestone 41) and only acts when it actually changed.
The very first check ever run has nothing to compare against, so it just
establishes a baseline rather than guessing a transition happened.
"""

import re
from typing import TYPE_CHECKING, Any

from app.skills.base import Skill

if TYPE_CHECKING:
    from app.core.siris import SirisAI

_PRESENCE_ENTITY_NAME = "presence"
_PRESENCE_EVENT_DESCRIPTION = "presence_changed"
_DEVICES_KEY = "presence:devices"
_SEEN_KEY = "presence:seen"
# 4.4: Dom's iPhone counts as home too, so the away protocol is only offered when both phones have left.
DEFAULT_ADDED = ["Dom's iPhone"]


def norm(name: Any) -> str:
    """A device name as UniFi might spell it either way: "Dom's iPhone", "Doms-iPhone" and "doms iphone" are one."""
    return re.sub(r"[^a-z0-9]", "", str(name or "").lower().replace("’", "'"))


def added(siris: "SirisAI") -> list[str]:
    """Phones added from SirisOS (4.4), on top of SIRISAI_PRESENCE_DEVICE_NAMES."""
    kept = siris.state.get(_DEVICES_KEY)
    return list(DEFAULT_ADDED) if kept is None else [str(n) for n in kept]


def set_added(siris: "SirisAI", names: list[str]) -> list[str]:
    clean = list(dict.fromkeys(n.strip()[:60] for n in names if n and n.strip()))[:12]
    siris.state.set(_DEVICES_KEY, clean)
    return clean


def counted(siris: "SirisAI") -> list[dict[str, Any]]:
    """Every phone that counts as someone home, where it comes from, and whether it was home at the last check."""
    seen = siris.state.get(_SEEN_KEY) or {}
    out, have = [], set()
    for name, source in [*((n, "settings") for n in siris.settings.presence_device_name_list()), *((n, "added") for n in added(siris))]:
        if norm(name) in have or not norm(name):
            continue
        have.add(norm(name))
        out.append({"name": name, "source": source, "home": seen.get(norm(name))})
    return out


class PresenceSkill(Skill):
    name = "presence_check"
    description = (
        "Check whether any known household device is currently connected to the network, and -- "
        "only on a change since the last check -- activate the configured home/away Home "
        "Assistant scene."
    )
    parameters = {"type": "object", "properties": {}, "required": []}

    def run(self, siris: "SirisAI", **kwargs: Any) -> Any:
        clients = siris.run_tool("unifi_list_clients", {})
        known = {norm(d["name"]) for d in counted(siris)}
        here = {norm(c.get(k)) for c in clients for k in ("name", "hostname") if c.get(k)}
        seen = {k: k in here for k in known}
        siris.state.set(_SEEN_KEY, seen)
        # Home while any counted phone is connected: away only once every one of them (Brad's and Dom's) has gone.
        currently_home = any(seen.values())

        result: dict[str, Any] = {"currently_home": currently_home, "changed": False, "scene_activated": None}

        previous = siris.events.recent_events(entity_name=_PRESENCE_ENTITY_NAME, limit=1)
        previously_home = previous[0]["data"]["home"] if previous and previous[0].get("data") else None

        if previously_home is None:
            # First-ever check: nothing to compare against, so this
            # establishes a baseline rather than assuming a transition.
            siris.events.log_event(_PRESENCE_EVENT_DESCRIPTION, entity_name=_PRESENCE_ENTITY_NAME,
                                   data={"home": currently_home, "baseline": True})
            return result

        if currently_home == previously_home:
            return result

        siris.events.log_event(_PRESENCE_EVENT_DESCRIPTION, entity_name=_PRESENCE_ENTITY_NAME, data={"home": currently_home})
        result["changed"] = True

        scene_entity_id = siris.settings.presence_home_scene if currently_home else siris.settings.presence_away_scene
        if scene_entity_id and siris.tools.get("home_assistant_call_service") is not None:
            state_label = "home" if currently_home else "away"
            siris.run_tool(
                "home_assistant_call_service",
                {"domain": "scene", "service": "turn_on", "entity_id": scene_entity_id},
                # Not a live human confirmation -- explicitly auto-confirmed
                # because this calls exactly one pre-configured scene the
                # user set up ahead of time via settings, not an arbitrary
                # command; setting SIRISAI_PRESENCE_*_SCENE *is* the
                # approval, the same reasoning SIRISAI_RESTRICTED_TOOLS_ENABLED
                # already uses to pre-approve specific tools via config
                # instead of a live confirmation every call.
                confirmed=True,
                trigger=(
                    f"presence_check: pre-configured {state_label} scene "
                    f"(SIRISAI_PRESENCE_{state_label.upper()}_SCENE)"
                ),
            )
            result["scene_activated"] = scene_entity_id

        from app.household import away

        away_result = away.on_change(siris, currently_home)
        if away_result is not None:
            result["away_mode"] = away_result

        if not currently_home and siris.tools.get("departure_check") is not None:
            # Everyone just left: anything open, coming up, or wet?
            try:
                result["departure"] = siris.run_tool("departure_check", {"notify": True}, trigger="presence_check: everyone left")
            except Exception:
                result["departure"] = None

        return result


def anyone_home(siris: "SirisAI") -> bool | None:
    """Whether the last presence check found someone home; None when
    presence has never been checked (or isn't configured)."""
    previous = siris.events.recent_events(entity_name=_PRESENCE_ENTITY_NAME, limit=1)
    if not previous or not previous[0].get("data"):
        return None
    return bool(previous[0]["data"].get("home"))
