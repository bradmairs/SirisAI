"""Protocol and guest-mode tools (app/protocols/service.py, app/core/guest.py).

run_protocol / undo_last_protocol are skills (they drive other tools through
siris.run_tool) and CONFIRM_REQUIRED: one confirmation covers every step, as
with scenes. Their dry_run shows exactly which steps would run and why."""

import re
from typing import TYPE_CHECKING, Any

from app.core import guest
from app.plugins.base import Tool, ToolPermission
from app.protocols import service
from app.skills.base import Skill

if TYPE_CHECKING:
    from app.core.siris import SirisAI

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,40}$")


class ListProtocolsTool(Tool):
    name = "list_protocols"
    description = (
        "List the protocols SirisAI can run -- named playbooks such as lockdown, away, guest -- with each one's steps."
    )
    permission = ToolPermission.READ
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, siris: "SirisAI") -> None:
        self._siris = siris

    def run(self, **kwargs: Any) -> Any:
        return [
            {"name": p["name"], "description": p["description"], "builtin": p["builtin"], "steps": [s.get("label") or s["tool"] for s in p["steps"]]}
            for p in service.all_protocols(self._siris).values()
        ]


class CreateProtocolTool(Tool):
    name = "create_protocol"
    description = (
        "Create or replace a protocol: a named playbook of tool calls run together (e.g. 'movie_night', 'bedtime'). "
        "Each step is {tool, arguments, label?, when?, undo?}; `when` may be nobody_home, someone_home, night, day, "
        "or {entity, state} / {entity, not_state}. Home Assistant steps may target {\"select\": {\"domain\": ..., "
        "\"device_class\": [...]}} instead of an entity id."
    )
    permission = ToolPermission.SAFE_ACTION
    parameters = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Short snake_case name, e.g. 'movie_night'."},
            "description": {"type": "string", "description": "What the protocol is for."},
            "steps": {"type": "array", "items": {"type": "object"}, "description": "Ordered steps."},
        },
        "required": ["name", "steps"],
    }

    def __init__(self, siris: "SirisAI") -> None:
        self._siris = siris

    def run(self, name: str = "", steps: list[dict[str, Any]] | None = None, description: str = "", **kwargs: Any) -> Any:
        name = name.strip().lower().replace(" ", "_")
        if not _NAME_RE.match(name):
            raise ValueError("Protocol names are 2-40 characters of lowercase letters, digits and underscores.")
        if not steps:
            raise ValueError("A protocol needs at least one step.")
        for step in steps:
            if not isinstance(step, dict) or not step.get("tool"):
                raise ValueError("Every step needs a 'tool'.")
            if step["tool"] in {"run_protocol", "undo_last_protocol"}:
                raise ValueError("A protocol can't run other protocols.")
            if self._siris.tools.get(step["tool"]) is None:
                raise ValueError(f"Unknown tool in step: {step['tool']}")
        return self._siris.protocols.save(name, description, steps)


class DeleteProtocolTool(Tool):
    name = "delete_protocol"
    description = "Delete a user-created protocol by name (built-in protocols can't be deleted)."
    permission = ToolPermission.SAFE_ACTION
    parameters = {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}

    def __init__(self, siris: "SirisAI") -> None:
        self._siris = siris

    def run(self, name: str = "", **kwargs: Any) -> Any:
        return {"name": name, "deleted": self._siris.protocols.delete(name.strip().lower().replace(" ", "_"))}


class RunProtocolSkill(Skill):
    name = "run_protocol"
    description = (
        "Run a protocol by name -- e.g. 'lockdown' (lock up, close the garage, lights on at night), 'away' (everything "
        "off and locked), 'guest' (guest mode + guest Wi-Fi), 'guest_off', or one the user created. Every step runs "
        "in order; the result can be reversed with undo_last_protocol."
    )
    parameters = {
        "type": "object",
        "properties": {"name": {"type": "string", "description": "Protocol name, from list_protocols."}},
        "required": ["name"],
    }

    def run(self, siris: "SirisAI", name: str = "", **kwargs: Any) -> Any:
        return service.run(siris, name, "run_protocol")

    def dry_run(self, siris: "SirisAI", name: str = "", **kwargs: Any) -> Any:
        return service.preview(siris, name)


class UndoLastProtocolSkill(Skill):
    name = "undo_last_protocol"
    description = "Reverse the most recent protocol run: put back the lights, locks, covers and modes it changed."
    parameters = {"type": "object", "properties": {}, "required": []}

    def run(self, siris: "SirisAI", **kwargs: Any) -> Any:
        return service.undo_last(siris, "undo_last_protocol")

    def dry_run(self, siris: "SirisAI", **kwargs: Any) -> Any:
        last = siris.state.get(service.LAST_RUN_KEY) or {}
        return {"protocol": last.get("protocol"), "undone": last.get("undone"), "would_run": list(reversed(last.get("undo") or []))}


class SetGuestModeTool(Tool):
    name = "set_guest_mode"
    description = (
        "Turn guest mode on or off. While on, SirisAI won't volunteer personal information (calendar, reminders, "
        "memories, email, health) because visitors may be listening. Optionally for a number of hours."
    )
    permission = ToolPermission.SAFE_ACTION
    parameters = {
        "type": "object",
        "properties": {
            "on": {"type": "boolean", "description": "True to turn guest mode on, false to turn it off."},
            "hours": {"type": "number", "description": "Optional: switch guest mode off by itself after this many hours."},
        },
        "required": ["on"],
    }

    def __init__(self, siris: "SirisAI") -> None:
        self._siris = siris

    def run(self, on: bool = True, hours: float | None = None, **kwargs: Any) -> Any:
        return guest.set_mode(self._siris, bool(on), hours)


class GuestModeStatusTool(Tool):
    name = "guest_mode_status"
    description = "Whether guest mode is on, since when, and until when."
    permission = ToolPermission.READ
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, siris: "SirisAI") -> None:
        self._siris = siris

    def run(self, **kwargs: Any) -> Any:
        return guest.status(self._siris)
