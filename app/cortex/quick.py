"""Plain home commands without the model (SirisAI 4.6; scenes, media,
the thermostat, "undo that" and learned phrases in 5.0).

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

Since 5.0 it also knows scenes ("movie time", "run the dinner scene"), media
("pause the lounge TV", "next song", "set the kitchen speaker volume to
30"), the thermostat ("set the heating to 21") and "undo that". And it
learns: a short request the model answered with the same single home action
three times becomes a quick command too (`learn`).

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


_SCENE = re.compile(rf"^{_POLITE}(?:(?:run|activate|start|set|put on|do) (?:the )?(?P<a>.+?)(?: scene| mode)?|(?P<b>.+? (?:time|mode)))(?: please)?[.!]?$", re.I)
_MEDIA = re.compile(rf"^{_POLITE}(?P<action>pause|play|resume|stop|mute|unmute) (?:the )?(?P<player>.+?){_TAIL}$", re.I)
_SKIP = re.compile(rf"^{_POLITE}(?P<action>next|skip|previous)(?: (?:track|song))?(?: on (?:the )?(?P<player>.+?))?{_TAIL}$", re.I)
_VOLUME = re.compile(rf"^{_POLITE}(?:set|turn) (?:the )?(?:volume (?:on|of) (?:the )?(?P<p1>.+?)|(?P<p2>.+?) volume) to (?P<pct>\d{{1,3}}) ?(?:%|percent)?{_TAIL}$", re.I)
_CLIMATE = re.compile(rf"^{_POLITE}set (?:the )?(?:heat|heating|heater|thermostat|aircon|air con|ac|a/c|temperature)(?: in (?:the )?(?P<room>.+?))? to (?P<deg>\d{{2}}(?:\.5)?)(?: ?(?:degrees|°c?|c))?{_TAIL}$", re.I)
_UNDO = re.compile(rf"^{_POLITE}(?:undo(?: that| it| the last (?:thing|one))?|take (?:that|it) back|put (?:that|it) back)[.!]?$", re.I)
LEARNABLE = {"home_assistant_call_service", "home_assistant_media_control", "run_scene"}
LEARNED_KEY = "quick:learned"
LEARN_AFTER = 3


@dataclass
class QuickPlan:
    tool: str
    arguments: dict[str, Any]
    reply: str | None  # None: the tool's own result says it (undo)


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s%]", " ", text.lower()).split())


def _client(siris: "SirisAI") -> Any:
    return getattr(siris.tools.get("home_assistant_resolve_entity"), "_client", None)


def _one(siris: "SirisAI", domain: str, name: str) -> tuple[str, str] | None:
    """(entity_id, friendly name) of the one entity in `domain` matching `name`."""
    from app.plugins.builtin.home_assistant import _entity_name_matches

    client = _client(siris)
    if client is None:
        return None
    states = client.list_states(domain=domain)
    words = [w for w in name.lower().split() if w not in _FILLER]
    found = [s for s in states if not words or _entity_name_matches(words, f"{s['entity_id']} {(s.get('attributes') or {}).get('friendly_name', '')}".lower())]
    if len(found) != 1:
        return None
    return found[0]["entity_id"], (found[0].get("attributes") or {}).get("friendly_name") or found[0]["entity_id"].split(".", 1)[1].replace("_", " ")


def _device(siris: "SirisAI", text: str) -> QuickPlan | None:
    command = parse(text)
    if command is None:
        return None
    found = resolve(siris, command)
    if found is None:
        return None
    domain, entity_id, friendly = found
    arguments: dict[str, Any] = {"domain": domain, "service": command.service, "entity_id": entity_id}
    if command.data:
        arguments["service_data"] = command.data
    label = friendly if friendly != entity_id else entity_id.split(".", 1)[1].replace("_", " ")
    if "brightness_pct" in command.data:
        return QuickPlan("home_assistant_call_service", arguments, f"Done, the {label} is at {command.data['brightness_pct']}%.")
    return QuickPlan("home_assistant_call_service", arguments, f"Done, the {label} is {command.service.removeprefix('turn_')}.")


def _scene(siris: "SirisAI", text: str) -> QuickPlan | None:
    m = _SCENE.match(text)
    if not m:
        return None
    name = _norm(m.group("a") or m.group("b") or "")
    bare = re.sub(r" (?:scene|mode|time)$", "", name)
    for scene in siris.scenes.list_scenes():
        if _norm(scene["name"]) in (name, bare) and siris.tools.get("run_scene") is not None:
            return QuickPlan("run_scene", {"scene_id": scene["id"]}, f"Done, {scene['name']} is on.")
    found = _one(siris, "scene", bare) if bare else None
    if found:
        return QuickPlan("home_assistant_call_service", {"domain": "scene", "service": "turn_on", "entity_id": found[0]}, f"Done, {found[1]} is on.")
    return None


def _media(siris: "SirisAI", text: str) -> QuickPlan | None:
    if siris.tools.get("home_assistant_media_control") is None:
        return None
    m = _MEDIA.match(text)
    if m:
        action = {"resume": "play"}.get(m.group("action").lower(), m.group("action").lower())
        found = _one(siris, "media_player", m.group("player"))
        if found:
            words = {"play": "playing", "pause": "paused", "stop": "stopped", "mute": "muted", "unmute": "unmuted"}[action]
            return QuickPlan("home_assistant_media_control", {"player": found[0], "action": action}, f"Done, the {found[1]} is {words}.")
        return None
    m = _SKIP.match(text)
    if m:
        action = "previous" if m.group("action").lower() == "previous" else "next"
        found = _one(siris, "media_player", m.group("player") or "")
        if found:
            return QuickPlan("home_assistant_media_control", {"player": found[0], "action": action}, f"Done, {action} on the {found[1]}.")
        return None
    m = _VOLUME.match(text)
    if m and 0 <= int(m.group("pct")) <= 100:
        found = _one(siris, "media_player", m.group("p1") or m.group("p2") or "")
        if found:
            return QuickPlan("home_assistant_media_control", {"player": found[0], "action": "volume", "volume_percent": int(m.group("pct"))},
                             f"Done, the {found[1]} is at {int(m.group('pct'))}% volume.")
    return None


def _climate(siris: "SirisAI", text: str) -> QuickPlan | None:
    m = _CLIMATE.match(text)
    if not m:
        return None
    degrees = float(m.group("deg"))
    if not 10 <= degrees <= 32:
        return None
    found = _one(siris, "climate", m.group("room") or "")
    if not found:
        return None
    shown = f"{degrees:g}"
    return QuickPlan("home_assistant_call_service", {"domain": "climate", "service": "set_temperature", "entity_id": found[0],
                                                     "service_data": {"temperature": degrees}}, f"Done, the {found[1]} is set to {shown}°.")


def _learned(siris: "SirisAI", text: str) -> QuickPlan | None:
    entry = (siris.state.get(LEARNED_KEY) or {}).get(_norm(text))
    if entry and entry.get("count", 0) >= LEARN_AFTER and entry.get("tool") in LEARNABLE:
        return QuickPlan(entry["tool"], entry["arguments"], "Done.")
    return None


def learn(siris: "SirisAI", text: str, tool: str, arguments: dict[str, Any]) -> None:
    """Remember a short request the model answered with one home action.
    The same words getting the same action LEARN_AFTER times makes it a
    quick command; different arguments start the count again."""
    phrase = _norm(text)
    if tool not in LEARNABLE or not phrase or len(phrase) > 60 or _NOT_SIMPLE.search(phrase):
        return
    learned = dict(siris.state.get(LEARNED_KEY) or {})
    entry = learned.get(phrase)
    if entry and entry.get("tool") == tool and entry.get("arguments") == arguments:
        entry = {**entry, "count": entry.get("count", 0) + 1}
    else:
        entry = {"tool": tool, "arguments": arguments, "count": 1}
    learned[phrase] = entry
    if len(learned) > 200:
        learned = dict(sorted(learned.items(), key=lambda kv: -kv[1].get("count", 0))[:200])
    siris.state.set(LEARNED_KEY, learned)


_GYM_LAST = re.compile(
    rf"^{_POLITE}what (?:did|weight did|weights did) i (?:lift|do|use|have|get|put)(?: last time)? (?:on|for|with) (?:the |my )?"
    rf"(?P<a>[a-z][a-z \-]{{1,40}}?)(?: last time| last session)?\??$"
    rf"|^{_POLITE}(?:what was|what's) my last (?P<b>[a-z][a-z \-]{{1,40}}?)(?: session| workout)?\??$", re.I)


def _gym_last(siris: "SirisAI", text: str) -> QuickPlan | None:
    """6.38: mid-workout, "what did I lift last time on incline press?" without the model."""
    m = _GYM_LAST.match(text)
    if not m or siris.tools.get("gym_sessions") is None:
        return None
    exercise = (m.group("a") or m.group("b") or "").strip()
    words = set(exercise.lower().split())
    if exercise in ("", "it", "that", "this") or words & {"monday", "tuesday", "wednesday", "thursday", "friday", "saturday",
                                                          "sunday", "today", "yesterday", "week", "weekend", "month", "gym"}:
        return None  # a day, not an exercise: that's gym_sessions with the model
    return QuickPlan("gym_sessions", {"exercise": exercise, "last_time": True}, None)


def plan(siris: "SirisAI", text: str) -> QuickPlan | None:
    """The tool call for a quick command, and what to say once done, or None
    to leave the message to the model."""
    if not siris.settings.quick_commands:
        return None
    text = " ".join(text.strip().split())
    if not text or len(text) > 80:
        return None
    if _UNDO.match(text):
        return QuickPlan("undo_last", {}, None) if siris.tools.get("undo_last") is not None else None
    excluded = siris.restricted_tool_names_to_exclude()
    found: QuickPlan | None = None
    for attempt in (_learned, _gym_last, _device, _scene, _media, _climate):
        if attempt is not _learned and _NOT_SIMPLE.search(text):
            break
        try:
            found = attempt(siris, text)
        except Exception:  # noqa: BLE001 - Home Assistant unreachable: let the model explain
            return None
        if found is not None:
            break
    if found is None or found.tool in excluded:
        return None
    tool = siris.tools.get(found.tool)
    if tool is None:
        return None
    from app.plugins.base import ToolPermission

    if tool.permission is ToolPermission.CONFIRM_REQUIRED and tool.needs_confirmation(found.arguments):
        return None
    return found
