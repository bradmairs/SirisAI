"""Undo by saying so (SirisAI 5.0): "undo that", "take that back".

Every action that can be put back is written to a short journal as it
runs, with the calls that would reverse it:

- a light, switch, fan or media player turned on or off, dimmed, or a
  thermostat set: back to the state it was in just before (read from Home
  Assistant first);
- a scene or protocol: the protocol's own undo;
- a reminder set: cancelled;
- an automation created: deleted (which asks first, like any deletion);
- a document filed: removed from the document inbox;
- a setting changed: put back.

`undo_last` (the tool, and "undo that" as a quick command) reverses the
newest entry of whoever's asking, from the last UNDO_HOURS (2). The
reversing calls go through the normal checks, so anything that needs
confirming still asks. Nothing is ever undone into something less safe:
the reverse of locking a door, closing the garage or arming the alarm is
never run (that's on purpose, and you're told so).
"""

from __future__ import annotations

import copy
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from app.core.user_context import get_current_user_id

if TYPE_CHECKING:
    from app.core.siris import SirisAI

logger = logging.getLogger(__name__)

KEY = "undo:journal"
KEEP = 30
UNDO_HOURS = 2
# Reverse calls that would make the house less secure: never run by undo.
UNSAFE = {("lock", "unlock"), ("lock", "open"), ("cover", "open_cover"), ("cover", "open"), ("alarm_control_panel", "alarm_disarm"),
          ("valve", "open_valve")}
_ON_OFF = {"light", "switch", "fan", "media_player", "input_boolean", "climate", "humidifier"}


def capture(siris: "SirisAI", name: str, arguments: dict[str, Any]) -> Any:
    """What's needed to reverse this call, read just before it runs."""
    if name != "home_assistant_call_service":
        return None
    entity = arguments.get("entity_id")
    if not isinstance(entity, str) or "." not in entity or entity in ("all", "none"):
        return None
    tool = siris.tools.get("home_assistant_call_service")
    client = getattr(tool, "_client", None)
    try:
        state = client.get_state_or_none(entity) if client is not None else None
        return copy.deepcopy(state)
    except Exception:  # noqa: BLE001 - no undo is better than no action
        return None


def _reverse_service_call(arguments: dict[str, Any], result: Any, prior: dict[str, Any] | None) -> tuple[str, list[dict[str, Any]]] | None:
    domain, service = str(arguments.get("domain") or ""), str(arguments.get("service") or "")
    entity = arguments.get("entity_id")
    if isinstance(result, dict) and result.get("resolved_entity_id"):
        entity = result["resolved_entity_id"]
    if not isinstance(entity, str) or prior is None:
        return None
    name = (prior.get("attributes") or {}).get("friendly_name") or entity.split(".", 1)[-1].replace("_", " ")
    attrs = prior.get("attributes") or {}
    was = prior.get("state")
    if domain == "climate" and service == "set_temperature" and attrs.get("temperature") is not None:
        return (f"set {name} to {arguments.get('service_data', {}).get('temperature')}°",
                [{"tool": "home_assistant_call_service", "arguments": {"domain": "climate", "service": "set_temperature", "entity_id": entity,
                                                                      "service_data": {"temperature": attrs["temperature"]}}}])
    if domain in ("lock", "cover", "alarm_control_panel", "valve"):
        inverse = {"lock": "unlock", "unlock": "lock", "close_cover": "open_cover", "open_cover": "close_cover",
                   "close_valve": "open_valve", "open_valve": "close_valve"}.get(service)
        if domain == "alarm_control_panel" and service.startswith("alarm_arm"):
            inverse = "alarm_disarm"
        if inverse is None or (domain, inverse) in UNSAFE:
            return (f"{service.replace('_', ' ')} {name}", [{"unsafe": f"I won't undo {service.replace('_', ' ')} on {name}: that would leave the house less secure."}])
        return (f"{service.replace('_', ' ')} {name}", [{"tool": "home_assistant_call_service",
                                                      "arguments": {"domain": domain, "service": inverse, "entity_id": entity}}])
    if domain in _ON_OFF and service in ("turn_on", "turn_off", "toggle") and was in ("on", "off", "playing", "paused", "idle", "heat", "cool", "auto"):
        back = "turn_off" if was == "off" else "turn_on"
        data: dict[str, Any] = {}
        if domain == "light" and was == "on" and isinstance(attrs.get("brightness"), (int, float)):
            data["brightness"] = int(attrs["brightness"])
        step: dict[str, Any] = {"domain": domain, "service": back, "entity_id": entity}
        if data:
            step["service_data"] = data
        words = {"turn_on": "turned on", "turn_off": "turned off", "toggle": "toggled"}[service]
        if (arguments.get("service_data") or {}).get("brightness_pct") is not None:
            words = f"set to {arguments['service_data']['brightness_pct']}%"
        return f"{words} {name}", [{"tool": "home_assistant_call_service", "arguments": step}]
    return None


def reverse(name: str, arguments: dict[str, Any], result: Any, prior: Any) -> tuple[str, list[dict[str, Any]]] | None:
    """(what was done, the steps that undo it) or None when it can't be undone."""
    if name == "home_assistant_call_service":
        return _reverse_service_call(arguments, result, prior)
    if name == "set_reminder" and isinstance(result, dict) and result.get("id") is not None:
        return (f"set a reminder: {result.get('message', '')}"[:120], [{"tool": "cancel_reminder", "arguments": {"reminder_id": result["id"]}}])
    if name == "home_assistant_create_automation" and isinstance(result, dict) and result.get("automation_id"):
        return (f"added the automation \"{arguments.get('alias', '')}\"",
                [{"tool": "home_assistant_delete_automation", "arguments": {"automation_id": result["automation_id"]}}])
    if name in ("run_protocol", "run_scene"):
        return (f"ran {arguments.get('name') or arguments.get('scene_id') or 'a scene'}", [{"protocol_undo": True}])
    return None


def record(siris: "SirisAI", name: str, arguments: dict[str, Any], result: Any, prior: Any, trigger: str | None) -> None:
    if name == "undo_last" or (trigger or "").startswith("undo:"):
        return
    try:
        found = reverse(name, arguments, result, prior)
    except Exception:  # noqa: BLE001
        logger.debug("Working out an undo failed", exc_info=True)
        return
    if found is None:
        return
    add(siris, found[0], found[1])


def add(siris: "SirisAI", label: str, steps: list[dict[str, Any]]) -> None:
    journal = list(siris.state.get(KEY) or [])
    journal.append({"at": datetime.now(UTC).isoformat(), "user": get_current_user_id(), "label": label, "steps": steps})
    siris.state.set(KEY, journal[-KEEP:])


def last(siris: "SirisAI", now: datetime | None = None) -> dict[str, Any] | None:
    now = now or datetime.now(UTC)
    user = get_current_user_id()
    for entry in reversed(siris.state.get(KEY) or []):
        if entry.get("user") == user and datetime.fromisoformat(entry["at"]) >= now - timedelta(hours=UNDO_HOURS):
            return entry
    return None


def history(siris: "SirisAI", now: datetime | None = None) -> list[dict[str, Any]]:
    """6.48: everything of yours that can still be undone, newest first, numbered (1 = the last)."""
    now = now or datetime.now(UTC)
    user = get_current_user_id()
    out = []
    for entry in reversed(siris.state.get(KEY) or []):
        at = datetime.fromisoformat(entry["at"])
        if entry.get("user") == user and at >= now - timedelta(hours=UNDO_HOURS):
            out.append({**entry, "number": len(out) + 1, "minutes_ago": round((now - at).total_seconds() / 60)})
    return out


def find(siris: "SirisAI", which: str | int | None, now: datetime | None = None) -> dict[str, Any] | None:
    """An entry by number (1 = the last), by "N minutes ago", or by words in what it did ("the lights")."""
    import re

    entries = history(siris, now)
    if not entries:
        return None
    if which in (None, "", 1, "1", "last", "that", "it"):
        return entries[0]
    text = str(which).strip().lower()
    if text.isdigit():
        n = int(text)
        return entries[n - 1] if 1 <= n <= len(entries) else None
    m = re.search(r"(\d+)\s*(?:min|minute)", text)
    if m:
        return min(entries, key=lambda e: abs(e["minutes_ago"] - int(m.group(1))))
    words = [w.rstrip("s") for w in re.findall(r"[a-z]+", text) if w not in {"the", "a", "my", "undo", "that", "ago", "from", "of"}]
    hits = [e for e in entries if words and all(w in e["label"].lower() for w in words)]
    return hits[0] if hits else None


def needs_confirming(siris: "SirisAI", now: datetime | None = None, entry: dict[str, Any] | None = None) -> bool:
    """Whether any reversing call would ask first (deleting an automation, say)."""
    entry = entry if entry is not None else last(siris, now)
    if entry is None:
        return False
    from app.plugins.base import ToolPermission

    for step in entry["steps"]:
        tool = siris.tools.get(step.get("tool") or "")
        if tool is not None and tool.permission is ToolPermission.CONFIRM_REQUIRED and tool.needs_confirmation(step["arguments"]):
            return True
    return False


def undo_last(siris: "SirisAI", now: datetime | None = None, confirmed: bool = False, which: str | int | None = None) -> dict[str, Any]:
    entry = find(siris, which, now) if which not in (None, "") else last(siris, now)
    if entry is None:
        if which not in (None, ""):
            return {"undone": None, "message": f"I couldn't find '{which}' among what can still be undone (the last two hours)."}
        return {"undone": None, "message": "There's nothing of yours from the last two hours to undo."}
    entry = {k: v for k, v in entry.items() if k not in ("number", "minutes_ago")}
    journal = [e for e in siris.state.get(KEY) or [] if e != entry]
    siris.state.set(KEY, journal)
    done, refused = [], []
    for step in entry["steps"]:
        if "unsafe" in step:
            refused.append(step["unsafe"])
            continue
        if step.get("protocol_undo"):
            from app.protocols import service as protocols

            done.append(protocols.undo_last(siris, "undo: asked to undo the last protocol"))
            continue
        if step.get("document_id") is not None:
            from app.documents import service as documents

            documents.delete(siris, int(step["document_id"]))
            done.append({"document_removed": step["document_id"]})
            continue
        if step.get("setting_change") is not None:
            from app.core import settings_overrides

            done.append(settings_overrides.undo(siris, int(step["setting_change"])))
            continue
        done.append(siris.run_tool(step["tool"], step["arguments"], confirmed=confirmed, trigger=f"undo: putting back \"{entry['label']}\""))
    message = f"Undone: {entry['label']}." if done else ""
    if refused:
        message = (message + " " + " ".join(refused)).strip()
    return {"undone": entry["label"], "message": message, "results": done}
