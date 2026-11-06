"""Automations made from plain speech, kept safe (SirisAI 4.6).

"When everyone leaves and the garage is open, tell me" becomes a Home
Assistant automation through home_assistant_create_automation, which always
asks first. This module adds two things around it:

- **A plain-English draft** of what the automation will do, shown in the
  approval ("When the garage door opens, if it's after sunset, notify
  Brad's phone"), so you approve what it does, not a block of YAML.
- **A floor it can't go under**: an automation runs on its own forever, so
  it may never unlock a door, open the garage, a gate, a valve or a cover,
  or disarm an alarm. Those stay one-off actions that ask every time. A
  draft that tries is refused with a reason, before anything is created.
"""

from __future__ import annotations

from typing import Any

# domain -> services an automation may never call by itself.
UNSAFE: dict[str, set[str]] = {
    "lock": {"unlock", "open"},
    "cover": {"open_cover", "open", "set_cover_position", "toggle"},
    "alarm_control_panel": {"alarm_disarm"},
    "valve": {"open_valve", "set_valve_position", "toggle"},
}
_SERVICE_KEYS = ("service", "action")


def _services(node: Any) -> list[str]:
    """Every "domain.service" an automation's actions would call, however
    deeply nested (choose, sequence, if/then, repeat)."""
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key in _SERVICE_KEYS and isinstance(value, str) and "." in value:
                found.append(value.strip().lower())
            else:
                found.extend(_services(value))
    elif isinstance(node, list):
        for value in node:
            found.extend(_services(value))
    return found


def problems(actions: list[dict[str, Any]]) -> list[str]:
    out = []
    for call in _services(actions):
        domain, _, service = call.partition(".")
        if service in UNSAFE.get(domain, set()):
            out.append(call)
        # homeassistant.turn_off/toggle on a lock or cover reaches the same place.
        if domain == "homeassistant" and service in {"toggle", "turn_off", "turn_on"}:
            targets = " ".join(_entities(actions))
            if any(f"{d}." in targets for d in UNSAFE):
                out.append(f"{call} on a lock, cover, valve or alarm")
    return sorted(set(out))


def check(actions: list[dict[str, Any]]) -> None:
    bad = problems(actions)
    if bad:
        raise ValueError(
            "An automation can't do this by itself: " + ", ".join(bad) + ". Unlocking, opening the garage or a "
            "gate, and disarming always ask each time, so do that as a one-off action instead. A safe "
            "alternative is to notify you, so you can do it with one tap."
        )


def _entities(node: Any) -> list[str]:
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "entity_id":
                found.extend([value] if isinstance(value, str) else [v for v in value or [] if isinstance(v, str)])
            else:
                found.extend(_entities(value))
    elif isinstance(node, list):
        for value in node:
            found.extend(_entities(value))
    return found


def _name(entity: str) -> str:
    return entity.split(".", 1)[-1].replace("_", " ")


def _names(node: Any) -> str:
    names = [_name(e) for e in _entities(node)]
    return " and ".join(names) if names else ""


def _trigger(t: dict[str, Any]) -> str:
    kind = t.get("platform") or t.get("trigger") or ""
    who = _names(t)
    if kind == "state":
        to = t.get("to")
        if to is not None:
            return f"{who or 'something'} becomes {to}"
        return f"{who or 'something'} changes"
    if kind == "time":
        return f"it's {t.get('at')}"
    if kind == "sun":
        offset = f" ({t['offset']})" if t.get("offset") else ""
        return f"at {t.get('event', 'sunset')}{offset}"
    if kind == "numeric_state":
        bits = [f"above {t['above']}" if "above" in t else "", f"below {t['below']}" if "below" in t else ""]
        return f"{who or 'a reading'} goes " + " and ".join(b for b in bits if b)
    if kind == "zone":
        return f"{who or 'someone'} {t.get('event', 'enters')}s {_name(str(t.get('zone', 'a zone')))}"
    if kind == "time_pattern":
        return "on a repeating schedule"
    return f"a {kind or 'trigger'} event happens"


def _condition(c: dict[str, Any]) -> str:
    kind = c.get("condition", "")
    if kind == "state":
        return f"{_names(c) or 'something'} is {c.get('state')}"
    if kind == "sun":
        return " and ".join(f"it's {k} {v}" for k, v in c.items() if k in ("after", "before"))
    if kind == "time":
        return " and ".join(f"it's {k} {v}" for k, v in c.items() if k in ("after", "before"))
    if kind == "numeric_state":
        bits = [f"above {c['above']}" if "above" in c else "", f"below {c['below']}" if "below" in c else ""]
        return f"{_names(c) or 'a reading'} is " + " and ".join(b for b in bits if b)
    return kind or "a condition holds"


def _action(a: dict[str, Any]) -> str:
    call = next((a[k] for k in _SERVICE_KEYS if isinstance(a.get(k), str)), None)
    if call is None:
        if "delay" in a:
            return f"wait {a['delay']}"
        nested = _services(a)
        return ", ".join(s.split(".", 1)[-1].replace("_", " ") for s in nested) or "do something"
    domain, _, service = call.partition(".")
    if domain == "notify":
        message = (a.get("data") or {}).get("message")
        return f"notify {service.replace('_', ' ')}" + (f' "{message}"' if message else "")
    target = _names(a)
    return f"{service.replace('_', ' ')} {target}".strip()


def summarize(arguments: dict[str, Any]) -> str:
    triggers = [t for t in arguments.get("trigger") or [] if isinstance(t, dict)]
    conditions = [c for c in arguments.get("condition") or [] if isinstance(c, dict)]
    actions = [a for a in arguments.get("action") or [] if isinstance(a, dict)]
    text = "When " + (" or ".join(_trigger(t) for t in triggers) or "it's triggered")
    if conditions:
        text += ", if " + " and ".join(_condition(c) for c in conditions)
    text += ", " + (", then ".join(_action(a) for a in actions) or "do nothing")
    return text + "."
