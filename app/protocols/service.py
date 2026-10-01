"""Protocols: named, cross-system playbooks -- "lockdown", "away", "guest"
-- run as one confirmed action, with conditional steps and a one-command undo.

Scenes (milestone 94) only replay Home Assistant service calls. A protocol's
step can be any registered tool (notify, set_guest_mode, a Docker restart, a
UniFi block...), can be conditional (`when`: "nobody_home", "someone_home",
"night", "day", or {"entity": ..., "state"/"not_state": ...}), and can target
Home Assistant entities by selector ({"select": {"domain": "cover",
"device_class": ["garage", "gate"]}}) instead of hard-coded ids.

Undo: before each Home Assistant step the affected entities' states are
recorded, and the inverse call for anything that changed is saved (a light
that was off gets turned back off, a door that was unlocked gets unlocked
again); set_guest_mode steps restore the previous mode; a step may also
declare its own `undo`. Only the most recent protocol run can be undone.

Built-in protocols live in code (`builtin_protocols`) and adapt to what's
configured -- a step whose tool isn't registered is left out. User-defined
ones are stored (the protocols table) and can override a built-in by name.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.core import clock
from app.db.models import Protocol
from app.voice.confirmations import SECURING_SERVICES

if TYPE_CHECKING:
    from app.core.siris import SirisAI

LAST_RUN_KEY = "protocol_last_run"
HA = "home_assistant_call_service"


_RESTORE = {
    "on": "turn_on",
    "off": "turn_off",
    "locked": "lock",
    "unlocked": "unlock",
    "open": "open_cover",
    "closed": "close_cover",
    "playing": "media_play",
    "paused": "media_pause",
    "idle": "turn_on",
}
_CHANGED_TO = {
    "turn_on": "on",
    "turn_off": "off",
    "lock": "locked",
    "unlock": "unlocked",
    "open_cover": "open",
    "close_cover": "closed",
    "toggle": None,
}


class ProtocolStore:
    def __init__(self, session_factory: sessionmaker) -> None:
        self._session_factory = session_factory

    def save(self, name: str, description: str, steps: list[dict[str, Any]]) -> dict[str, Any]:
        with self._session_factory() as session:
            row = session.scalars(select(Protocol).where(Protocol.name == name)).first()
            if row is None:
                row = Protocol(name=name, description=description, steps=json.dumps(steps))
                session.add(row)
            else:
                row.description, row.steps = description, json.dumps(steps)
            session.commit()
            return self._serialize(row)

    def get(self, name: str) -> dict[str, Any] | None:
        with self._session_factory() as session:
            row = session.scalars(select(Protocol).where(Protocol.name == name)).first()
            return self._serialize(row) if row is not None else None

    def list(self) -> list[dict[str, Any]]:
        with self._session_factory() as session:
            return [self._serialize(r) for r in session.scalars(select(Protocol).order_by(Protocol.name)).all()]

    def delete(self, name: str) -> bool:
        with self._session_factory() as session:
            row = session.scalars(select(Protocol).where(Protocol.name == name)).first()
            if row is None:
                return False
            session.delete(row)
            session.commit()
            return True

    @staticmethod
    def _serialize(row: Protocol) -> dict[str, Any]:
        return {"name": row.name, "description": row.description, "steps": json.loads(row.steps), "builtin": False}


def _ha(domain: str, service: str, entity: Any, label: str, when: Any = None) -> dict[str, Any]:
    step: dict[str, Any] = {"tool": HA, "arguments": {"domain": domain, "service": service, "entity_id": entity}, "label": label}
    if when:
        step["when"] = when
    return step


def builtin_protocols(siris: "SirisAI") -> dict[str, dict[str, Any]]:
    garage = {"select": {"domain": "cover", "device_class": ["garage", "gate"]}}
    guest_wifi = siris.settings.guest_wifi_entity_id
    protocols = {
        "lockdown": {
            "description": "Secure the house: lock every lock, close garage doors and gates, lights on at night, tell everyone.",
            "steps": [
                _ha("lock", "lock", "all", "Lock every lock"),
                _ha("cover", "close_cover", garage, "Close garage doors and gates"),
                _ha("light", "turn_on", "all", "All lights on (deterrent)", when="night"),
                {"tool": "notify", "arguments": {"title": "SirisAI: lockdown", "message": "Lockdown protocol ran: locks locked, garage closed.", "priority": "high"}, "label": "Notify"},
            ],
        },
        "away": {
            "description": "Everyone's out: lights and media off, doors locked, garage closed.",
            "steps": [
                _ha("light", "turn_off", "all", "Lights off"),
                _ha("media_player", "turn_off", "all", "Media off"),
                _ha("lock", "lock", "all", "Lock every lock"),
                _ha("cover", "close_cover", garage, "Close garage doors and gates"),
                {"tool": "notify", "arguments": {"title": "SirisAI: away", "message": "Away protocol ran: house secured."}, "label": "Notify"},
            ],
        },
        "guest": {
            "description": "Visitors over: guest mode on for 24 hours (no personal info volunteered), guest Wi-Fi on.",
            "steps": [
                {"tool": "set_guest_mode", "arguments": {"on": True, "hours": 24}, "label": "Guest mode on"},
                *([_ha("switch", "turn_on", guest_wifi, "Guest Wi-Fi on")] if guest_wifi else []),
            ],
        },
        "guest_off": {
            "description": "Visitors gone: guest mode off, guest Wi-Fi off.",
            "steps": [
                {"tool": "set_guest_mode", "arguments": {"on": False}, "label": "Guest mode off"},
                *([_ha("switch", "turn_off", guest_wifi, "Guest Wi-Fi off")] if guest_wifi else []),
            ],
        },
    }
    result = {}
    for name, proto in protocols.items():
        steps = [s for s in proto["steps"] if siris.tools.get(s["tool"]) is not None]
        if steps:
            result[name] = {"name": name, "description": proto["description"], "steps": steps, "builtin": True}
    return result


def all_protocols(siris: "SirisAI") -> dict[str, dict[str, Any]]:
    protocols = builtin_protocols(siris)
    for proto in siris.protocols.list():
        protocols[proto["name"]] = proto
    return protocols


def get_protocol(siris: "SirisAI", name: str) -> dict[str, Any] | None:
    return all_protocols(siris).get(name.strip().lower().replace(" ", "_"))


def condition_met(siris: "SirisAI", when: Any) -> tuple[bool, str]:
    """(met, why) for a step's `when`."""
    from app.skills.builtin.presence import anyone_home

    if when in (None, ""):
        return True, "always"
    if when in ("nobody_home", "someone_home"):
        home = anyone_home(siris)
        if home is None:
            return False, "presence isn't known"
        met = (not home) if when == "nobody_home" else home
        return met, "nobody is home" if not home else "someone is home"
    if when in ("night", "day"):
        hour = clock.now_local(siris.settings.timezone).hour
        start, end = siris.settings.night_start_hour, siris.settings.night_end_hour
        night = hour >= start or hour < end if start > end else start <= hour < end
        return (night if when == "night" else not night), ("it's night" if night else "it's daytime")
    if isinstance(when, dict) and when.get("entity"):
        client = siris._home_assistant_client
        if client is None:
            return False, "Home Assistant isn't configured"
        state = (client.get_state_or_none(when["entity"]) or {}).get("state")
        if "state" in when:
            return state == when["state"], f"{when['entity']} is {state}"
        if "not_state" in when:
            return state != when["not_state"], f"{when['entity']} is {state}"
    return False, f"unknown condition {when!r}"


def _expand_entities(siris: "SirisAI", entity: Any) -> Any:
    """Resolve a {"select": {...}} entity selector to a list of entity ids."""
    if not isinstance(entity, dict) or "select" not in entity:
        return entity
    client = siris._home_assistant_client
    if client is None:
        return []
    spec = entity["select"]
    classes = spec.get("device_class")
    classes = {classes} if isinstance(classes, str) else set(classes or [])
    return [
        s["entity_id"]
        for s in client.list_states(spec.get("domain"))
        if not classes or (s.get("attributes") or {}).get("device_class") in classes
    ]


def _prior_states(siris: "SirisAI", domain: str, entity: Any) -> dict[str, str]:
    client = siris._home_assistant_client
    if client is None:
        return {}
    if entity == "all":
        return {s["entity_id"]: s.get("state") for s in client.list_states(domain)}
    ids = entity if isinstance(entity, list) else [entity] if entity else []
    states = {}
    for entity_id in ids:
        found = client.get_state_or_none(entity_id)
        if found is not None:
            states[entity_id] = found.get("state")
    return states


def _undo_for(siris: "SirisAI", step: dict[str, Any], arguments: dict[str, Any], prior: dict[str, Any]) -> list[dict[str, Any]]:
    if step.get("undo"):
        return [step["undo"]]
    if step["tool"] == "set_guest_mode":
        return [{"tool": "set_guest_mode", "arguments": {"on": bool(prior.get("guest_on"))}}]
    if step["tool"] != HA:
        return []
    target = _CHANGED_TO.get(arguments.get("service"))
    undo = []
    for entity_id, state in prior.get("states", {}).items():
        restore = _RESTORE.get(state or "")
        if restore is None or state == target:
            continue
        domain = entity_id.split(".", 1)[0]
        undo.append({"tool": HA, "arguments": {"domain": domain, "service": restore, "entity_id": entity_id}})
    return undo


def run(siris: "SirisAI", name: str, trigger: str) -> dict[str, Any]:
    from app.core import guest

    proto = get_protocol(siris, name)
    if proto is None:
        raise ValueError(f"Unknown protocol: {name}")
    results: list[dict[str, Any]] = []
    undo: list[dict[str, Any]] = []
    for index, step in enumerate(proto["steps"], start=1):
        label = step.get("label") or step["tool"]
        met, why = condition_met(siris, step.get("when"))
        if not met:
            results.append({"step": label, "status": "skipped", "reason": why})
            continue
        if siris.tools.get(step["tool"]) is None:
            results.append({"step": label, "status": "skipped", "reason": f"{step['tool']} isn't available"})
            continue
        arguments = dict(step.get("arguments") or {})
        prior: dict[str, Any] = {}
        if step["tool"] == HA:
            arguments["entity_id"] = _expand_entities(siris, arguments.get("entity_id"))
            if arguments["entity_id"] == []:
                results.append({"step": label, "status": "skipped", "reason": "no matching entities"})
                continue
            prior["states"] = _prior_states(siris, arguments.get("domain", ""), arguments["entity_id"])
        elif step["tool"] == "set_guest_mode":
            prior["guest_on"] = guest.is_on(siris)
        try:
            output = siris.run_tool(
                step["tool"],
                arguments,
                # The one confirmation of running this named protocol covers
                # every step it's documented as containing, as with scenes.
                confirmed=True,
                trigger=f"{trigger}: protocol '{proto['name']}' step {index} ({label})",
            )
        except Exception as exc:  # one failed step doesn't abandon the rest (a lockdown should still lock the doors)
            results.append({"step": label, "status": "error", "error": str(exc)[:200]})
            continue
        results.append({"step": label, "status": "ok", "result": output})
        undo.extend(_undo_for(siris, step, arguments, prior))

    siris.state.set(
        LAST_RUN_KEY,
        {"protocol": proto["name"], "ran_at": datetime.now(UTC).isoformat(), "undo": undo, "undone": False},
    )
    return {"protocol": proto["name"], "steps": results, "undo_available": bool(undo)}


def preview(siris: "SirisAI", name: str) -> dict[str, Any]:
    proto = get_protocol(siris, name)
    if proto is None:
        return {"protocol": name, "found": False, "steps": []}
    steps = []
    for step in proto["steps"]:
        met, why = condition_met(siris, step.get("when"))
        arguments = dict(step.get("arguments") or {})
        if step["tool"] == HA:
            arguments["entity_id"] = _expand_entities(siris, arguments.get("entity_id"))
        steps.append({"step": step.get("label") or step["tool"], "tool": step["tool"], "arguments": arguments, "will_run": met, "because": why})
    return {"protocol": proto["name"], "found": True, "description": proto["description"], "steps": steps}


def undo_last(siris: "SirisAI", trigger: str) -> dict[str, Any]:
    last = siris.state.get(LAST_RUN_KEY)
    if not last or last.get("undone") or not last.get("undo"):
        return {"undone": False, "reason": "Nothing to undo."}
    results = []
    for step in reversed(last["undo"]):
        try:
            siris.run_tool(step["tool"], step["arguments"], confirmed=True, trigger=f"{trigger}: undo protocol '{last['protocol']}'")
            results.append({"tool": step["tool"], "arguments": step["arguments"], "status": "ok"})
        except Exception as exc:
            results.append({"tool": step["tool"], "arguments": step["arguments"], "status": "error", "error": str(exc)[:200]})
    last["undone"] = True
    siris.state.set(LAST_RUN_KEY, last)
    return {"undone": True, "protocol": last["protocol"], "steps": results}


def voice_confirmable(siris: "SirisAI", name: str, blocked_domains: set[str]) -> bool:
    """A protocol may be confirmed by voice unless one of its steps would be
    blocked by voice on its own (e.g. an unlock); securing steps (lock,
    close) are fine."""
    proto = get_protocol(siris, name)
    if proto is None:
        return False
    for step in proto["steps"]:
        if step["tool"] != HA:
            if step["tool"] in blocked_domains:
                return False
            continue
        args = step.get("arguments") or {}
        if str(args.get("domain", "")).lower() in blocked_domains and args.get("service") not in SECURING_SERVICES:
            return False
    return True
