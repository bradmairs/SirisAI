"""Plain home commands without the model (SirisAI 4.6).

"Turn off the kitchen lights", "switch the fan on", "dim the lounge to 30%"
are the commonest things said to SirisAI and the slowest to answer: one or
more model calls to pick a tool and then write "Done". This recognises a
handful of exact shapes, finds the device, and runs the same
home_assistant_call_service tool the model would have (same audit log, same
check afterwards), in a fraction of the time. It also keeps those commands
working when the model is down.

It only acts when everything is clear-cut; anything else goes to the model
as before:

- the sentence is exactly one of the shapes below (nothing else in it, no
  "and", "all", "every", "unless", times or conditions);
- exactly one light, switch or fan matches the name;
- the call needs no confirmation (SIRISAI_HOME_ASSISTANT_NO_CONFIRM_DOMAINS).
  Locks, covers and alarms are never in the quick list at all.

SIRISAI_QUICK_COMMANDS=false turns it off.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.core.siris import SirisAI

DOMAINS = ("light", "switch", "fan")
_POLITE = r"(?:(?:please|hey siris|siris|ok siris)[, ]+)?"
_TAIL = r"(?:[, ]+please)?[.!]?"
_SHAPES = [
    re.compile(rf"^{_POLITE}(?:turn|switch|put) (?P<state>on|off) (?:the )?(?P<name>.+?){_TAIL}$", re.I),
    re.compile(rf"^{_POLITE}(?:turn|switch|put) (?:the )?(?P<name>.+?) (?P<state>on|off){_TAIL}$", re.I),
    re.compile(rf"^{_POLITE}(?:dim|set|turn|bring) (?:the )?(?P<name>.+?) (?:to|at|down to|up to) (?P<pct>\d{{1,3}}) ?(?:%|percent){_TAIL}$", re.I),
]
# Words that mean more than one plain command: leave those to the model.
_NOT_SIMPLE = re.compile(r"\b(and|all|every|everything|unless|until|when|if|then|after|before|in \d|for \d|at \d|o'clock|tomorrow|tonight)\b", re.I)
_FILLER = {"the", "my", "a", "please"}


@dataclass
class QuickCommand:
    name: str
    service: str
    data: dict[str, Any] = field(default_factory=dict)


def parse(text: str) -> QuickCommand | None:
    text = " ".join(text.strip().split())
    if not text or len(text) > 80 or _NOT_SIMPLE.search(text):
        return None
    for shape in _SHAPES:
        m = shape.match(text)
        if not m:
            continue
        name = " ".join(w for w in m.group("name").lower().split() if w not in _FILLER)
        if not name:
            return None
        if m.groupdict().get("pct"):
            pct = int(m.group("pct"))
            if not 0 <= pct <= 100:
                return None
            if pct == 0:
                return QuickCommand(name, "turn_off")
            return QuickCommand(name, "turn_on", {"brightness_pct": pct})
        return QuickCommand(name, f"turn_{m.group('state').lower()}")
    return None


def resolve(siris: "SirisAI", command: QuickCommand) -> tuple[str, str, str] | None:
    """(domain, entity_id, friendly name) when exactly one device matches, else None."""
    from app.plugins.builtin.home_assistant import _entity_name_matches

    tool = siris.tools.get("home_assistant_resolve_entity")
    client = getattr(tool, "_client", None)
    if client is None:
        return None
    domains = ("light",) if "brightness_pct" in command.data else DOMAINS
    words = command.name.split()
    matches = []
    for state in client.list_states():
        domain = state["entity_id"].split(".", 1)[0]
        if domain not in domains:
            continue
        friendly = (state.get("attributes") or {}).get("friendly_name", "")
        if _entity_name_matches(words, f"{state['entity_id']} {friendly}".lower()):
            matches.append((domain, state["entity_id"], friendly or state["entity_id"]))
    if len(matches) != 1:
        return None
    return matches[0]


def plan(siris: "SirisAI", text: str) -> tuple[dict[str, Any], str] | None:
    """The tool arguments for a quick command, and what to say once done,
    or None to leave the message to the model."""
    if not siris.settings.quick_commands:
        return None
    command = parse(text)
    if command is None:
        return None
    tool = siris.tools.get("home_assistant_call_service")
    if tool is None or tool.name in siris.restricted_tool_names_to_exclude():
        return None
    try:
        found = resolve(siris, command)
    except Exception:  # noqa: BLE001 - Home Assistant unreachable: let the model explain
        return None
    if found is None:
        return None
    domain, entity_id, friendly = found
    arguments: dict[str, Any] = {"domain": domain, "service": command.service, "entity_id": entity_id}
    if command.data:
        arguments["service_data"] = command.data
    needs = getattr(tool, "needs_confirmation", None)
    if needs is not None and needs(arguments):
        return None
    label = friendly if friendly != entity_id else entity_id.split(".", 1)[1].replace("_", " ")
    if "brightness_pct" in command.data:
        reply = f"Done, the {label} is at {command.data['brightness_pct']}%."
    else:
        reply = f"Done, the {label} is {command.service.removeprefix('turn_')}."
    return arguments, reply
