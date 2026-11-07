"""Editing protocols from SirisOS (hub v1): what a step can do, and checking a protocol before
it's saved.

The editor offers a fixed set of steps, so a protocol saved over HTTP can only do what these
allow:

- Home Assistant: an action from ACTIONS on one entity, a list of them, every entity of the
  domain ("all"), or a selector ({"select": {"domain": "cover", "device_class": [...]}}),
  with brightness or temperature where the action takes one.
- notify: a title, a message and a priority.
- set_guest_mode: on (for some hours) or off.

A step using any other tool (one made in chat with create_protocol) is kept as long as it
comes back unchanged; it can't be added or altered here. Running a protocol still needs the
user's confirmation, as before.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from app.protocols.service import HA, all_protocols, builtin_protocols

if TYPE_CHECKING:
    from app.core.siris import SirisAI

NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,40}$")
RESERVED = {"undo"}   # /protocols/undo is the undo endpoint

# Home Assistant actions a step can take, per domain, and the one extra field each may set.
ACTIONS: dict[str, dict[str, str | None]] = {
    "light": {"turn_on": "brightness_pct", "turn_off": None, "toggle": None},
    "switch": {"turn_on": None, "turn_off": None, "toggle": None},
    "fan": {"turn_on": None, "turn_off": None},
    "media_player": {"turn_on": None, "turn_off": None, "media_play": None, "media_pause": None},
    "climate": {"turn_on": None, "turn_off": None, "set_temperature": "temperature"},
    "cover": {"open_cover": None, "close_cover": None},
    "lock": {"lock": None, "unlock": None},
    "scene": {"turn_on": None},
    "script": {"turn_on": None},
}
FIELD_RANGE = {"brightness_pct": (1, 100), "temperature": (10, 32)}
WHEN = {"nobody_home", "someone_home", "night", "day"}
PRIORITIES = {"min", "low", "default", "high", "urgent"}
EDITABLE_TOOLS = {HA, "notify", "set_guest_mode"}


class Invalid(ValueError):
    pass


def entities(siris: "SirisAI") -> list[dict[str, Any]]:
    """Home Assistant entities a step can target, by domain and name."""
    client = siris._home_assistant_client
    if client is None:
        return []
    try:
        states = client.list_states()
    except Exception:  # noqa: BLE001 - the editor still works with "all" and selectors
        return []
    out = []
    for s in states:
        domain = s["entity_id"].split(".", 1)[0]
        if domain in ACTIONS:
            attrs = s.get("attributes") or {}
            out.append({"entity_id": s["entity_id"], "name": attrs.get("friendly_name") or s["entity_id"], "domain": domain,
                        "device_class": attrs.get("device_class")})
    return sorted(out, key=lambda e: (e["domain"], e["name"].lower()))


def catalogue(siris: "SirisAI") -> dict[str, Any]:
    builtins = builtin_protocols(siris)
    protocols = []
    for name, p in sorted(all_protocols(siris).items()):
        protocols.append({**p, "builtin": bool(p.get("builtin")), "customised": not p.get("builtin") and name in builtins})
    return {"protocols": protocols, "actions": {d: list(s) for d, s in ACTIONS.items()}, "entities": entities(siris),
            "home_assistant": siris.tools.get(HA) is not None}


def _target(domain: str, entity: Any) -> Any:
    if entity == "all":
        return entity
    ids = entity if isinstance(entity, list) else [entity] if isinstance(entity, str) else None
    if ids is not None:
        if not ids or len(ids) > 50 or not all(isinstance(i, str) and re.fullmatch(rf"{domain}\.[a-z0-9_]+", i) for i in ids):
            raise Invalid(f"A {domain} step needs {domain} entities (like {domain}.lounge), 'all', or a selector.")
        return ids[0] if isinstance(entity, str) else ids
    if isinstance(entity, dict) and set(entity) == {"select"} and isinstance(entity["select"], dict):
        spec = entity["select"]
        classes = spec.get("device_class") or []
        classes = [classes] if isinstance(classes, str) else classes
        if spec.get("domain") != domain or not all(isinstance(c, str) and re.fullmatch(r"[a-z_]{1,30}", c) for c in classes):
            raise Invalid("A selector picks entities of the step's own domain, by device class.")
        return {"select": {"domain": domain, **({"device_class": classes} if classes else {})}}
    raise Invalid("Pick what the step acts on.")


def _when(when: Any) -> Any:
    if when in (None, "", "always"):
        return None
    if when in WHEN:
        return when
    if isinstance(when, dict) and isinstance(when.get("entity"), str) and len(when) == 2 and \
            any(isinstance(when.get(k), str) for k in ("state", "not_state")):
        return when
    raise Invalid(f"Unknown condition {when!r}.")


def _norm(step: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in step.items() if v not in (None, "", {}, [])}


def _step(siris: "SirisAI", raw: Any, kept: list[dict[str, Any]]) -> dict[str, Any]:
    if not isinstance(raw, dict) or not isinstance(raw.get("tool"), str):
        raise Invalid("Every step needs a tool.")
    tool, args = raw["tool"], raw.get("arguments") or {}
    label = str(raw.get("label") or "").strip()[:80]
    same = next((k for k in kept if _norm(raw) == _norm(k)), None)
    if same is not None:
        return same   # unchanged from what's saved (or the built-in): kept exactly, chat-only parts and all
    if raw.get("undo"):
        raise Invalid("A step's own undo can only be set in chat.")
    if tool not in EDITABLE_TOOLS:
        raise Invalid(f"'{tool}' steps can't be added or changed here: ask Siris in chat.")
    if siris.tools.get(tool) is None:
        raise Invalid(f"'{tool}' isn't available on this SirisAI.")
    step: dict[str, Any] = {"tool": tool}
    if tool == HA:
        domain, service = args.get("domain"), args.get("service")
        if domain not in ACTIONS or service not in ACTIONS[domain]:
            raise Invalid(f"Home Assistant steps can't {service!r} a {domain!r}.")
        clean: dict[str, Any] = {"domain": domain, "service": service, "entity_id": _target(domain, args.get("entity_id"))}
        field, data = ACTIONS[domain][service], args.get("service_data") or {}
        if field and field in data:
            lo, hi = FIELD_RANGE[field]
            value = data[field]
            if not isinstance(value, (int, float)) or not lo <= value <= hi:
                raise Invalid(f"{field.replace('_', ' ')} is {lo} to {hi}.")
            clean["service_data"] = {field: value}
        step["arguments"] = clean
    elif tool == "notify":
        message = str(args.get("message") or "").strip()
        if not message:
            raise Invalid("A notification needs a message.")
        clean = {"message": message[:500]}
        if args.get("title"):
            clean["title"] = str(args["title"]).strip()[:100]
        if args.get("priority"):
            if args["priority"] not in PRIORITIES:
                raise Invalid(f"Priority is one of {', '.join(sorted(PRIORITIES))}.")
            clean["priority"] = args["priority"]
        step["arguments"] = clean
    else:   # set_guest_mode
        on = args.get("on")
        if not isinstance(on, bool):
            raise Invalid("Guest mode is on or off.")
        clean = {"on": on}
        if on and args.get("hours") is not None:
            hours = args["hours"]
            if not isinstance(hours, (int, float)) or not 1 <= hours <= 168:
                raise Invalid("Guest mode lasts 1 to 168 hours.")
            clean["hours"] = hours
        step["arguments"] = clean
    if label:
        step["label"] = label
    when = _when(raw.get("when"))
    if when is not None:
        step["when"] = when
    return step


def save(siris: "SirisAI", name: str, description: str, steps: list[Any]) -> dict[str, Any]:
    """Check a protocol and save it (a built-in's name saves your version over it)."""
    name = name.strip().lower().replace(" ", "_").replace("-", "_")
    if not NAME_RE.match(name) or name in RESERVED:
        raise Invalid("Names are 2-41 characters: lowercase letters, digits and underscores, starting with a letter.")
    if not steps:
        raise Invalid("A protocol needs at least one step.")
    if len(steps) > 30:
        raise Invalid("A protocol has at most 30 steps.")
    current = all_protocols(siris).get(name)
    kept = list(current["steps"]) if current else []
    clean = [_step(siris, s, kept) for s in steps]
    saved = siris.protocols.save(name, str(description or "").strip()[:300], clean)
    from app.protocols.service import is_active, notifies

    return {**saved, "builtin": False, "customised": name in builtin_protocols(siris), "active": is_active(siris, name), "notify": notifies(siris, name)}


def delete(siris: "SirisAI", name: str) -> dict[str, Any]:
    """Delete your protocol; for a built-in you'd changed, the original comes back."""
    name = name.strip().lower()
    deleted = siris.protocols.delete(name)
    return {"name": name, "deleted": deleted, "restored_builtin": deleted and name in builtin_protocols(siris)}
