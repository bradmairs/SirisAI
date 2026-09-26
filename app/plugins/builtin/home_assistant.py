"""Home Assistant tools.

Deliberately generic -- domain/service/entity_id, the same shape Home
Assistant's own service-call model uses -- rather than one tool per device,
so this works for whatever's actually in a given homelab without code
changes. Registered only when SIRISAI_HOME_ASSISTANT_BASE_URL and
SIRISAI_HOME_ASSISTANT_TOKEN are both set (see app/core/siris.py); until
then SirisAI simply doesn't offer these tools to the model.
"""

import difflib
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from app.home_assistant.client import HomeAssistantClient
from app.memory.events import EventMemoryStore
from app.plugins.base import Tool, ToolPermission, ToolVerification

# How close a name_hint word has to be to some word actually in an entity's
# id/friendly name to count as a match when it isn't a literal substring
# (a user-requested hardening pass: matching used to require every word to
# appear verbatim -- "lounge lamp" would never match a "Lounge Light" entity,
# and a one-letter typo like "loung" would never match "lounge" at all).
# 0.8 is deliberately conservative: it catches a typo or a simple plural
# ("lamp" vs "lamps", ratio ~0.89) without also catching genuinely different
# words that happen to share a few letters. This is still literal/fuzzy
# *spelling* tolerance, not semantic understanding -- "lamp" won't match
# "light" (they don't look alike), only sound-alike/spelled-alike variants
# of the words already present. Real synonym matching would need embedding
# search, a much larger change; this is scoped to what "a slight variation"
# actually asked for.
_FUZZY_MATCH_CUTOFF = 0.8


def _entity_name_matches(name_hint_words: list[str], haystack: str) -> bool:
    """Whether every word in `name_hint_words` is present in `haystack`
    (entity_id + friendly name, already lowercased) -- literally, as
    before, or as a close spelling match against one of haystack's own
    words. Order-independent either way, same as the original
    substring-only check."""
    haystack_words = haystack.replace(".", " ").replace("_", " ").replace("-", " ").split()
    for word in name_hint_words:
        if word in haystack:
            continue
        if difflib.get_close_matches(word, haystack_words, n=1, cutoff=_FUZZY_MATCH_CUTOFF):
            continue
        return False
    return True

# Result validation (a hardening pass, milestone 65): services with an
# unambiguous expected end state, so HomeAssistantCallServiceTool.verify()
# below can check "did this actually happen" rather than just "did the API
# call return 200" -- the same gap DockerRestartContainerTool.verify()
# (milestone 38) closed for container restarts, applied here to physical
# devices, which can go unresponsive/offline in a way the API call alone
# doesn't surface. Deliberately not exhaustive: a scene activation, a
# non-boolean set_temperature, or a notify call has no single "correct"
# resulting state to check against, so those are left to raise
# NotImplementedError rather than this guessing at a shape that doesn't fit.
_EXPECTED_STATE_BY_SERVICE: dict[str, str] = {
    "turn_on": "on",
    "turn_off": "off",
    "lock": "locked",
    "unlock": "unlocked",
    "open_cover": "open",
    "close_cover": "closed",
    "arm_away": "armed_away",
    "arm_home": "armed_home",
    "arm_night": "armed_night",
    "disarm": "disarmed",
}

# How long verify() waits for a device's reported state to catch up with
# the call (found live, 2026-09-19: a real light accepted turn_on and was
# "on" 2 seconds later, but verify() read it back instantly, saw "off",
# and recorded verified=False -- a false alarm on every toggle of a
# device that's slow to report). Polls up to _VERIFY_ATTEMPTS times,
# _VERIFY_POLL_SECONDS apart, and stops the moment the state matches, so
# a fast device costs nothing extra and only a genuinely-wrong state
# (offline/unavailable) pays the full wait.
_VERIFY_ATTEMPTS = 4
_VERIFY_POLL_SECONDS = 1.0

# Always require confirmation for these domains, even when
# SIRISAI_HOME_ASSISTANT_NO_CONFIRM_DOMAINS is "*" (every other domain) --
# a direct user request: physical security (a door lock, and a garage
# door, which Home Assistant models as a `cover` entity) should never be
# auto-approved just because "everything else" is. Not configurable, same
# reasoning as HomeAssistantCreateAutomationTool/DeleteAutomationTool's
# unconditional CONFIRM_REQUIRED.
_ALWAYS_CONFIRM_DOMAINS = {"lock", "cover"}


class HomeAssistantListEntitiesTool(Tool):
    name = "home_assistant_list_entities"
    description = (
        "List Home Assistant entities and their current state. Optionally filter by "
        "domain (e.g. 'light', 'switch', 'sensor', 'climate') to narrow down a large "
        "home. Use this to discover entity_ids before calling home_assistant_get_state "
        "or home_assistant_call_service."
    )
    permission = ToolPermission.READ
    parameters = {
        "type": "object",
        "properties": {
            "domain": {
                "type": "string",
                "description": "Optional Home Assistant domain to filter by, e.g. 'light' or 'switch'.",
            },
        },
        "required": [],
    }

    def __init__(self, client: HomeAssistantClient) -> None:
        self._client = client

    def run(self, domain: str | None = None, **kwargs: Any) -> Any:
        return [
            {
                "entity_id": s["entity_id"],
                "name": s.get("attributes", {}).get("friendly_name"),
                "state": s["state"],
            }
            for s in self._client.list_states(domain=domain)
        ]


class HomeAssistantGetStateTool(Tool):
    name = "home_assistant_get_state"
    description = "Get the current state and attributes of one Home Assistant entity."
    permission = ToolPermission.READ
    parameters = {
        "type": "object",
        "properties": {
            "entity_id": {
                "type": "string",
                "description": "The entity to look up, e.g. 'light.living_room'.",
            },
        },
        "required": ["entity_id"],
    }

    def __init__(self, client: HomeAssistantClient) -> None:
        self._client = client

    def run(self, entity_id: str = "", **kwargs: Any) -> Any:
        return self._client.get_state(entity_id)


class HomeAssistantResolveEntityTool(Tool):
    name = "home_assistant_resolve_entity"
    description = (
        "Find Home Assistant entities matching a loose name (e.g. 'office light' or 'front "
        "door'), checking both entity_id and friendly name. Tolerant of small spelling "
        "variations and plurals (e.g. 'lounge lamps' still matches an entity named 'Lounge "
        "Lamp') -- but not synonyms for a different word entirely. Returns every match, not "
        "just the best one -- use this before calling home_assistant_call_service on something "
        "the user referred to by description rather than an exact entity_id, and if "
        "`ambiguous` comes back true, ask which one was meant rather than guessing."
    )
    permission = ToolPermission.READ
    parameters = {
        "type": "object",
        "properties": {
            "name_hint": {
                "type": "string",
                "description": "Loose name or description to match, e.g. 'office light'.",
            },
            "domain": {
                "type": "string",
                "description": "Optional Home Assistant domain to narrow the search, e.g. 'light'.",
            },
        },
        "required": ["name_hint"],
    }

    def __init__(self, client: HomeAssistantClient) -> None:
        self._client = client

    def run(self, name_hint: str = "", domain: str | None = None, **kwargs: Any) -> Any:
        needle_words = name_hint.lower().split()
        matches = []
        for state in self._client.list_states(domain=domain):
            friendly_name = state.get("attributes", {}).get("friendly_name", "")
            haystack = f"{state['entity_id']} {friendly_name}".lower()
            if _entity_name_matches(needle_words, haystack):
                matches.append(
                    {
                        "entity_id": state["entity_id"],
                        "friendly_name": friendly_name or None,
                        "state": state["state"],
                    }
                )
        return {"matches": matches, "ambiguous": len(matches) > 1}


class HomeAssistantCallServiceTool(Tool):
    name = "home_assistant_call_service"
    description = (
        "Call any Home Assistant service to control a device -- e.g. domain='light', "
        "service='turn_on', entity_id='light.living_room'. The same request however it's "
        "phrased -- 'turn off the lounge lamps' and 'turn the lounge lamps off' both mean "
        "service='turn_off' -- what matters is the verb and the device, not their order. Use "
        "home_assistant_resolve_entity first if you don't know the exact entity_id -- if you "
        "give one that doesn't exist anyway, this still checks for a single close match "
        "before giving up. Extra service-specific fields (like 'brightness_pct' or "
        "'temperature') go in service_data."
    )
    # Arbitrary/unbounded: this can call *any* Home Assistant service --
    # including ones with real physical consequences (unlocking a door,
    # disarming an alarm) that a routine "turn on the light" doesn't carry.
    # CONFIRM_REQUIRED by default; needs_confirmation() below narrows that
    # per-call via a configurable domain allowlist (a direct user request
    # -- lights/switches/etc shouldn't need confirming every time --
    # rather than removing confirmation outright, which would drop the
    # safety net for locks/alarms/covers too).
    permission = ToolPermission.CONFIRM_REQUIRED
    parameters = {
        "type": "object",
        "properties": {
            "domain": {
                "type": "string",
                "description": "Service domain, e.g. 'light', 'switch', 'climate'.",
            },
            "service": {
                "type": "string",
                "description": "Service to call, e.g. 'turn_on', 'turn_off', 'toggle', 'set_temperature'.",
            },
            "entity_id": {
                "type": "string",
                "description": "The entity to target, e.g. 'light.living_room'.",
            },
            "service_data": {
                "type": "object",
                "description": "Extra service-specific fields, e.g. {'brightness_pct': 50}.",
            },
        },
        "required": ["domain", "service"],
    }

    def __init__(
        self,
        client: HomeAssistantClient,
        events: EventMemoryStore,
        no_confirm_domains: list[str] | None = None,
    ) -> None:
        self._client = client
        self._events = events
        self._no_confirm_domains = set(no_confirm_domains or [])

    def needs_confirmation(self, arguments: dict[str, Any]) -> bool:
        """Skips confirmation for a call whose `domain` is in
        SIRISAI_HOME_ASSISTANT_NO_CONFIRM_DOMAINS -- everything else
        (an unconfigured/empty allowlist means everything) still needs
        it, same as before this existed. A single "*" in the allowlist
        means every domain, EXCEPT _ALWAYS_CONFIRM_DOMAINS (lock, cover),
        which always need it regardless -- that floor can't be bypassed
        by "*" or by explicitly listing "lock"/"cover" either."""
        domain = arguments.get("domain")
        if domain in _ALWAYS_CONFIRM_DOMAINS:
            return True
        if "*" in self._no_confirm_domains:
            return False
        return domain not in self._no_confirm_domains

    def run(
        self,
        domain: str = "",
        service: str = "",
        entity_id: str | None = None,
        service_data: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Any:
        target_entity_id = entity_id
        resolution_note: str | None = None
        # Fuzzy fallback (a user-requested hardening pass): the model is
        # supposed to call home_assistant_resolve_entity first when it
        # doesn't already know the exact entity_id, but when it skips
        # that and guesses wrong, this used to just fail/no-op against
        # Home Assistant with no recovery. Only ever substitutes when
        # exactly one same-domain entity is a close spelling match --
        # zero or multiple candidates fall through to the original
        # (still-wrong) entity_id, same "let it fail/no-op rather than
        # guess among several" caution home_assistant_resolve_entity's
        # own `ambiguous` flag already uses.
        if entity_id and self._client.get_state_or_none(entity_id) is None:
            target_entity_id, resolution_note = self._resolve_fallback(domain, entity_id)

        result = self._client.call_service(
            domain, service, entity_id=target_entity_id, **(service_data or {})
        )
        # Behavioral pattern anomalies (milestone 41): log this as an
        # Event, grouped by "{domain}.{service}" + entity_id (e.g.
        # "light.turn_off" on "light.office"), so EventMemoryStore's
        # existing timing-baseline machinery (37) can learn when this
        # normally happens and flag it when it doesn't -- "the office
        # lights are usually off by 11" is the same shape of anomaly as
        # "the backup didn't run", once the action is visible as a
        # recurring Event signature. No new detection logic needed:
        # event_timing_check already does the rest once this exists.
        self._events.log_event(
            f"{domain}.{service}",
            entity_name=target_entity_id,
            source="home_assistant_call_service",
        )
        if resolution_note:
            # Wrapped rather than returned bare: callers (verify() here,
            # and anyone reading /siris/activity) need to know the
            # substitution happened and what it actually ran against.
            # Unwrapped in the common case (no substitution) so every
            # existing caller/test of the normal path is unaffected.
            return {"result": result, "resolved_entity_id": target_entity_id, "note": resolution_note}
        return result

    def _resolve_fallback(self, domain: str, entity_id: str) -> tuple[str, str | None]:
        """entity_id doesn't exist -- see if exactly one same-domain
        entity is a close spelling match (same tokenize-and-compare
        helper home_assistant_resolve_entity uses), same discipline: one
        match substitutes, zero or several leave `entity_id` untouched
        rather than guess."""
        states = self._client.list_states(domain=domain)
        needle_words = entity_id.lower().replace(".", " ").replace("_", " ").replace("-", " ").split()
        candidates = [
            state["entity_id"]
            for state in states
            if _entity_name_matches(
                needle_words,
                f"{state['entity_id']} {state.get('attributes', {}).get('friendly_name', '')}".lower(),
            )
        ]
        if len(candidates) == 1:
            return candidates[0], f"'{entity_id}' doesn't exist -- matched it to '{candidates[0]}' instead."
        return entity_id, None

    def verify(self, arguments: dict[str, Any], result: Any) -> ToolVerification:
        """Independent check that the call actually took effect, not just
        that Home Assistant's API accepted it -- an unreachable/offline
        device is the common real failure mode this exists for: the
        service call can return success while the entity's state never
        actually changes. Only checked when both a concrete `entity_id`
        and a service in `_EXPECTED_STATE_BY_SERVICE` are given; anything
        else honestly raises `NotImplementedError` (this tool's base
        class's own "doesn't support verify" convention) rather than
        claiming to check something with no single correct end state.

        Checks the entity the call actually ran against, not necessarily
        what was originally asked for: when run()'s fuzzy fallback
        substituted a different entity_id, `result` carries
        `resolved_entity_id` -- checking the original (nonexistent) one
        would always "fail" even though the real target changed state
        exactly as expected."""
        entity_id = (
            result.get("resolved_entity_id")
            if isinstance(result, dict) and "resolved_entity_id" in result
            else arguments.get("entity_id")
        )
        expected_state = _EXPECTED_STATE_BY_SERVICE.get(arguments.get("service", ""))
        if entity_id is None or expected_state is None:
            raise NotImplementedError(
                f"{self.name} does not support verify for this domain/service"
            )

        actual_state = None
        for attempt in range(_VERIFY_ATTEMPTS):
            actual_state = self._client.get_state(entity_id).get("state")
            if actual_state == expected_state:
                break
            if attempt < _VERIFY_ATTEMPTS - 1:
                time.sleep(_VERIFY_POLL_SECONDS)
        if actual_state == expected_state:
            return ToolVerification(
                verified=True,
                detail=f"'{entity_id}' is now '{actual_state}', as expected.",
            )
        return ToolVerification(
            verified=False,
            detail=f"'{entity_id}' is '{actual_state}', not the expected '{expected_state}'.",
        )


def _normalize_automation_actions(actions: list[Any]) -> list[Any]:
    """Home Assistant wants `{"service": "light.turn_off", ...}`. The model
    often writes `{"domain": "light", "service": "turn_off", ...}` (found
    live: HA rejected it with a 400). Rewrite that shape; leave anything
    else exactly as given."""
    fixed: list[Any] = []
    for item in actions:
        if isinstance(item, dict) and "domain" in item and isinstance(item.get("service"), str) and "." not in item["service"]:
            item = {**item}
            item["service"] = f"{item.pop('domain')}.{item['service']}"
        fixed.append(item)
    return fixed


class HomeAssistantCreateAutomationTool(Tool):
    name = "home_assistant_create_automation"
    description = (
        "Create a new Home Assistant automation -- a standing rule that keeps firing on its own "
        "(e.g. 'if motion is detected and it's dark, turn on the hallway light'), not a one-off "
        "action. trigger/condition/action use Home Assistant's own automation YAML shape (the same "
        "one used in automations.yaml or the UI's 'edit in YAML' view) -- pass them through as you "
        "already know them, they aren't re-validated here. Use home_assistant_list_entities first "
        "to find real entity_ids for the trigger/condition/action to reference. Use "
        "home_assistant_list_entities with domain='automation' to see automations that already exist."
    )
    # CONFIRM_REQUIRED, and deliberately not eligible for the
    # SIRISAI_HOME_ASSISTANT_NO_CONFIRM_DOMAINS allowlist that
    # HomeAssistantCallServiceTool.needs_confirmation() checks (this is a
    # separate Tool class, so it just gets Tool.needs_confirmation()'s
    # default of True, unconditionally) -- creating a standing automation
    # is a different order of consequence than one call_service
    # invocation. A one-off "turn on the kitchen light" being allowlisted
    # says nothing about whether "always turn on the kitchen light when
    # motion is detected, forever, until someone notices and removes it"
    # should skip a human actually looking at it first.
    permission = ToolPermission.CONFIRM_REQUIRED
    parameters = {
        "type": "object",
        "properties": {
            "alias": {
                "type": "string",
                "description": "The automation's display name in Home Assistant.",
            },
            "trigger": {
                "type": "array",
                "items": {"type": "object"},
                "description": "One or more Home Assistant triggers, e.g. "
                "[{'platform': 'state', 'entity_id': 'binary_sensor.hallway_motion', 'to': 'on'}].",
            },
            "action": {
                "type": "array",
                "items": {"type": "object"},
                "description": "One or more Home Assistant actions, e.g. "
                "[{'service': 'light.turn_on', 'target': {'entity_id': 'light.hallway'}}]. Put the "
                "domain and service together as 'light.turn_on' in one 'service' key -- never separate "
                "'domain' and 'service' keys.",
            },
            "condition": {
                "type": "array",
                "items": {"type": "object"},
                "description": "Optional conditions that must also hold, e.g. "
                "[{'condition': 'sun', 'after': 'sunset'}].",
            },
            "mode": {
                "type": "string",
                "description": "Optional: how overlapping runs are handled -- 'single' (default), "
                "'restart', 'queued', or 'parallel'.",
            },
            "description": {
                "type": "string",
                "description": "Optional longer description of what this does.",
            },
        },
        "required": ["alias", "trigger", "action"],
    }

    def __init__(self, client: HomeAssistantClient) -> None:
        self._client = client

    def run(
        self,
        alias: str = "",
        trigger: list[dict[str, Any]] | None = None,
        action: list[dict[str, Any]] | None = None,
        condition: list[dict[str, Any]] | None = None,
        mode: str | None = None,
        description: str | None = None,
        **kwargs: Any,
    ) -> Any:
        return self._client.create_automation(
            alias,
            trigger or [],
            _normalize_automation_actions(action or []),
            condition=condition,
            mode=mode,
            description=description,
        )

    def verify(self, arguments: dict[str, Any], result: Any) -> ToolVerification:
        """Same gap as HomeAssistantCallServiceTool.verify() (milestone
        65): create_automation() hitting HA's config-editor endpoint
        without raising doesn't prove the automation actually persisted
        -- that endpoint isn't part of HA's documented REST API (see
        app/home_assistant/client.py's module docstring), so a
        malformed payload or a version mismatch is exactly the kind of
        thing that could accept the write and not actually register it.
        Re-lists domain='automation' entities and checks the new
        automation_id shows up among their `id` attributes -- the same
        lookup this client's own docstring describes as the only way to
        find an automation's id from its entity."""
        automation_id = (
            result.get("automation_id") if isinstance(result, dict) else None
        )
        if not automation_id:
            raise NotImplementedError(
                f"{self.name} does not support verify without an automation_id"
            )

        # A new automation registers a moment after HA accepts it, so poll
        # instead of reading once (same race as call_service's verify()).
        found = False
        for attempt in range(_VERIFY_ATTEMPTS):
            automations = self._client.list_states(domain="automation")
            found = any(a.get("attributes", {}).get("id") == automation_id for a in automations)
            if found:
                break
            if attempt < _VERIFY_ATTEMPTS - 1:
                time.sleep(_VERIFY_POLL_SECONDS)
        if found:
            return ToolVerification(
                verified=True, detail=f"Automation {automation_id} confirmed present."
            )
        return ToolVerification(
            verified=False,
            detail=f"Automation {automation_id} not found among registered automations.",
        )


class HomeAssistantGetLogbookTool(Tool):
    name = "home_assistant_get_logbook"
    description = (
        "Look up Home Assistant's logbook -- what happened and, where Home Assistant knows it, "
        "why (which automation or entity caused it) -- not just current state. Use this to "
        "answer 'why did X happen' questions (e.g. 'why did the hallway light turn on') by "
        "inspecting recent history for that entity, or omit entity_id for the whole home's "
        "recent activity. entity_id must be the real Home Assistant id (e.g. 'light.hallway'), "
        "not a friendly name -- use home_assistant_list_entities first if you don't know it."
    )
    permission = ToolPermission.READ
    parameters = {
        "type": "object",
        "properties": {
            "entity_id": {
                "type": "string",
                "description": "Optional entity to filter to, e.g. 'light.hallway'. Omit for everything.",
            },
            "hours": {
                "type": "integer",
                "description": "How far back to look, in hours. Defaults to 24.",
            },
        },
        "required": [],
    }

    def __init__(self, client: HomeAssistantClient) -> None:
        self._client = client

    def run(self, entity_id: str | None = None, hours: int = 24, **kwargs: Any) -> Any:
        since = datetime.now(UTC) - timedelta(hours=hours)
        return self._client.get_logbook(entity_id=entity_id, since=since)


class HomeAssistantGetHistoryTool(Tool):
    name = "home_assistant_get_history"
    description = (
        "Get how one Home Assistant entity's state has changed over time (e.g. 'how has the "
        "temperature changed today') -- a raw value series, not discrete named events with a cause "
        "(use home_assistant_get_logbook for that). entity_id must be the real Home Assistant id "
        "(e.g. 'sensor.living_room_temperature'), not a friendly name."
    )
    permission = ToolPermission.READ
    parameters = {
        "type": "object",
        "properties": {
            "entity_id": {
                "type": "string",
                "description": "The entity to look up, e.g. 'sensor.living_room_temperature'.",
            },
            "hours": {
                "type": "integer",
                "description": "How far back to look, in hours. Defaults to 24.",
            },
        },
        "required": ["entity_id"],
    }

    def __init__(self, client: HomeAssistantClient) -> None:
        self._client = client

    def run(self, entity_id: str = "", hours: int = 24, **kwargs: Any) -> Any:
        return self._client.get_history(entity_id, hours=hours)


class HomeAssistantRenderTemplateTool(Tool):
    name = "home_assistant_render_template"
    description = (
        "Evaluate a Home Assistant Jinja2 template against its current state and return the "
        "rendered text -- read-only, for computing something no single existing tool returns "
        "directly (e.g. an average across several sensors), using Home Assistant's own templating "
        "(https://www.home-assistant.io/docs/configuration/templating/) rather than fetching every "
        "entity and computing it yourself."
    )
    permission = ToolPermission.READ
    parameters = {
        "type": "object",
        "properties": {
            "template": {
                "type": "string",
                "description": "The Jinja2 template to render, e.g. "
                "\"{{ states('sensor.living_room_temperature') }}\".",
            },
        },
        "required": ["template"],
    }

    def __init__(self, client: HomeAssistantClient) -> None:
        self._client = client

    def run(self, template: str = "", **kwargs: Any) -> Any:
        return {"rendered": self._client.render_template(template)}


class HomeAssistantDeleteAutomationTool(Tool):
    name = "home_assistant_delete_automation"
    description = (
        "Delete a Home Assistant automation, using its automation_id -- not the entity_id. Find it "
        "via home_assistant_get_state on the automation's entity_id (e.g. 'automation.hallway_light'); "
        "the id is in its attributes."
    )
    # Same reasoning as HomeAssistantCreateAutomationTool: removing a
    # standing rule is a different order of consequence than a one-off
    # call_service, and deliberately not eligible for the
    # SIRISAI_HOME_ASSISTANT_NO_CONFIRM_DOMAINS allowlist -- this is a
    # separate Tool class with no needs_confirmation() override.
    permission = ToolPermission.CONFIRM_REQUIRED
    parameters = {
        "type": "object",
        "properties": {
            "automation_id": {
                "type": "string",
                "description": "The automation's config id, from its entity's 'id' attribute.",
            },
        },
        "required": ["automation_id"],
    }

    def __init__(self, client: HomeAssistantClient) -> None:
        self._client = client

    def run(self, automation_id: str = "", **kwargs: Any) -> Any:
        return self._client.delete_automation(automation_id)

    def verify(self, arguments: dict[str, Any], result: Any) -> ToolVerification:
        """Mirror of HomeAssistantCreateAutomationTool.verify(): a 200
        from HA's config-editor DELETE endpoint doesn't prove the
        automation is actually gone, for the same reason a 200 from its
        create counterpart doesn't prove it's actually there. Re-lists
        domain='automation' entities and confirms the deleted
        automation_id no longer shows up among their `id` attributes."""
        automation_id = arguments.get("automation_id")
        if not automation_id:
            raise NotImplementedError(
                f"{self.name} does not support verify without an automation_id"
            )

        automations = self._client.list_states(domain="automation")
        still_present = any(
            a.get("attributes", {}).get("id") == automation_id for a in automations
        )
        if not still_present:
            return ToolVerification(
                verified=True, detail=f"Automation {automation_id} confirmed removed."
            )
        return ToolVerification(
            verified=False,
            detail=f"Automation {automation_id} is still present after delete.",
        )


# -- To-do lists, calendars and media players (Home Assistant already has all
# three; SirisAI had no way to reach them -- "what's on my calendar?" said the
# calendar wasn't configured, and "to-do list" answered with goals).


def _resolve_entity_id(client: HomeAssistantClient, domain: str, hint: str | None, what: str) -> str:
    """The entity_id for `hint` (an id or a loose name) within `domain`. With
    no hint and exactly one entity, that one; anything ambiguous raises with
    the candidates so the model can ask instead of guessing."""
    states = client.list_states(domain=domain)
    if not states:
        raise ValueError(f"Home Assistant has no {what}.")
    if hint:
        hint_l = hint.strip().lower()
        for st in states:
            if st["entity_id"].lower() == hint_l:
                return st["entity_id"]
        words = hint_l.split()
        matches = [
            st
            for st in states
            if _entity_name_matches(words, f"{st['entity_id']} {st.get('attributes', {}).get('friendly_name', '')}".lower())
        ]
    else:
        matches = states
    if len(matches) == 1:
        return matches[0]["entity_id"]
    options = ", ".join(
        f"{m['entity_id']} ({m.get('attributes', {}).get('friendly_name') or m['state']})" for m in (matches or states)
    )
    raise ValueError(f"Which {what}? Options: {options}")


class HomeAssistantGetTodoItemsTool(Tool):
    name = "home_assistant_get_todo_items"
    description = (
        "Get the open items on a Home Assistant to-do or shopping list. With no list named, returns every list "
        "and its open items. Use for 'what's on my to-do list', 'what do I need from the shops'."
    )
    permission = ToolPermission.READ
    parameters = {
        "type": "object",
        "properties": {
            "list_name": {"type": "string", "description": "Optional list entity_id or loose name, e.g. 'shopping'."},
            "include_completed": {"type": "boolean", "description": "Also return completed items. Defaults to false."},
        },
        "required": [],
    }

    def __init__(self, client: HomeAssistantClient) -> None:
        self._client = client

    def run(self, list_name: str | None = None, include_completed: bool = False, **kwargs: Any) -> Any:
        if list_name:
            entity_ids = [_resolve_entity_id(self._client, "todo", list_name, "to-do list")]
        else:
            entity_ids = [s["entity_id"] for s in self._client.list_states(domain="todo")]
        status = None if include_completed else "needs_action"
        return [
            {"list": eid, "items": self._client.get_todo_items(eid, status=status)}
            for eid in entity_ids
        ]


class HomeAssistantAddTodoItemTool(Tool):
    name = "home_assistant_add_todo_item"
    description = (
        "Add an item to a Home Assistant to-do or shopping list, e.g. item='milk', list_name='shopping'. "
        "If there is only one list it is used automatically."
    )
    permission = ToolPermission.SAFE_ACTION
    parameters = {
        "type": "object",
        "properties": {
            "item": {"type": "string", "description": "The item to add, e.g. 'milk'."},
            "list_name": {"type": "string", "description": "Optional list entity_id or loose name."},
        },
        "required": ["item"],
    }

    def __init__(self, client: HomeAssistantClient) -> None:
        self._client = client

    def run(self, item: str = "", list_name: str | None = None, **kwargs: Any) -> Any:
        entity_id = _resolve_entity_id(self._client, "todo", list_name, "to-do list")
        self._client.add_todo_item(entity_id, item)
        return {"added": item, "list": entity_id}


class HomeAssistantCompleteTodoItemTool(Tool):
    name = "home_assistant_complete_todo_item"
    description = "Tick off an item on a Home Assistant to-do or shopping list by its text (as returned by home_assistant_get_todo_items)."
    permission = ToolPermission.SAFE_ACTION
    parameters = {
        "type": "object",
        "properties": {
            "item": {"type": "string", "description": "The item's exact text."},
            "list_name": {"type": "string", "description": "Optional list entity_id or loose name."},
        },
        "required": ["item"],
    }

    def __init__(self, client: HomeAssistantClient) -> None:
        self._client = client

    def run(self, item: str = "", list_name: str | None = None, **kwargs: Any) -> Any:
        entity_id = _resolve_entity_id(self._client, "todo", list_name, "to-do list")
        self._client.complete_todo_item(entity_id, item)
        return {"completed": item, "list": entity_id}


class HomeAssistantGetCalendarEventsTool(Tool):
    name = "home_assistant_get_calendar_events"
    description = (
        "Get upcoming events from the calendars in Home Assistant (all of them unless one is named), for the next "
        "N days starting now, soonest first. Use for 'what's on my calendar today/tomorrow/this week'."
    )
    permission = ToolPermission.READ
    parameters = {
        "type": "object",
        "properties": {
            "days": {"type": "integer", "description": "How many days ahead to look, 1-30. Defaults to 1 (today)."},
            "calendar": {"type": "string", "description": "Optional calendar entity_id or loose name."},
        },
        "required": [],
    }

    def __init__(self, client: HomeAssistantClient, timezone: str = "UTC") -> None:
        self._client = client
        self._timezone = timezone

    def run(self, days: int = 1, calendar: str | None = None, **kwargs: Any) -> Any:
        from app.core.clock import get_zone

        zone = get_zone(self._timezone)
        now = datetime.now(zone)
        days = max(1, min(int(days), 30))
        end = (now + timedelta(days=days)).replace(hour=23, minute=59, second=59) if days == 1 else now + timedelta(days=days)
        if calendar:
            calendars = [{"entity_id": _resolve_entity_id(self._client, "calendar", calendar, "calendar"), "name": calendar}]
        else:
            calendars = self._client.list_calendars()
        events: list[dict[str, Any]] = []
        for cal in calendars:
            for ev in self._client.get_calendar_events(cal["entity_id"], now, end):
                start = ev.get("start", {})
                stamp = start.get("dateTime") or start.get("date") or ""
                events.append(
                    {
                        "calendar": cal.get("name") or cal["entity_id"],
                        "summary": ev.get("summary"),
                        "start": stamp,
                        "all_day": "date" in start and "dateTime" not in start,
                        "location": ev.get("location"),
                    }
                )
        events.sort(key=lambda e: e["start"])
        return events


_MEDIA_ACTIONS = {
    "play": "media_play",
    "pause": "media_pause",
    "stop": "media_stop",
    "next": "media_next_track",
    "previous": "media_previous_track",
    "on": "turn_on",
    "off": "turn_off",
    "mute": "volume_mute",
    "unmute": "volume_mute",
    "volume": "volume_set",
    "play_media": "play_media",
}


class HomeAssistantListMediaPlayersTool(Tool):
    name = "home_assistant_list_media_players"
    description = "List media players (speakers, TVs) with their state, volume and what is playing. Use before controlling one."
    permission = ToolPermission.READ
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, client: HomeAssistantClient) -> None:
        self._client = client

    def run(self, **kwargs: Any) -> Any:
        out = []
        for st in self._client.list_states(domain="media_player"):
            attrs = st.get("attributes", {})
            out.append(
                {
                    "entity_id": st["entity_id"],
                    "name": attrs.get("friendly_name"),
                    "state": st["state"],
                    "volume_percent": round(attrs["volume_level"] * 100) if isinstance(attrs.get("volume_level"), (int, float)) else None,
                    "playing": attrs.get("media_title"),
                    "source": attrs.get("source"),
                }
            )
        return out


class HomeAssistantMediaControlTool(Tool):
    name = "home_assistant_media_control"
    description = (
        "Control a media player (speaker or TV): play, pause, stop, next, previous, volume (with volume_percent), "
        "mute, unmute, on, off, or play_media (with media_content_id and media_content_type such as 'music'). The "
        "player can be an entity_id or a loose name like 'lounge speaker'. Note: playing something by name (like "
        "'jazz') needs a content id the player understands; many players (e.g. Echo) can't be asked for a genre."
    )
    permission = ToolPermission.SAFE_ACTION
    parameters = {
        "type": "object",
        "properties": {
            "player": {"type": "string", "description": "Media player entity_id or loose name."},
            "action": {"type": "string", "enum": sorted(_MEDIA_ACTIONS), "description": "What to do."},
            "volume_percent": {"type": "integer", "description": "For action=volume: 0-100."},
            "media_content_id": {"type": "string", "description": "For action=play_media."},
            "media_content_type": {"type": "string", "description": "For action=play_media, e.g. 'music'."},
        },
        "required": ["player", "action"],
    }

    def __init__(self, client: HomeAssistantClient) -> None:
        self._client = client

    def run(
        self,
        player: str = "",
        action: str = "",
        volume_percent: int | None = None,
        media_content_id: str | None = None,
        media_content_type: str | None = None,
        **kwargs: Any,
    ) -> Any:
        service = _MEDIA_ACTIONS.get(action)
        if service is None:
            raise ValueError(f"Unknown action '{action}'. Options: {', '.join(sorted(_MEDIA_ACTIONS))}")
        entity_id = _resolve_entity_id(self._client, "media_player", player, "media player")
        data: dict[str, Any] = {}
        if action == "volume":
            if volume_percent is None or not 0 <= volume_percent <= 100:
                raise ValueError("volume needs volume_percent between 0 and 100.")
            data["volume_level"] = volume_percent / 100
        elif action in ("mute", "unmute"):
            data["is_volume_muted"] = action == "mute"
        elif action == "play_media":
            if not media_content_id or not media_content_type:
                raise ValueError("play_media needs media_content_id and media_content_type.")
            data["media_content_id"] = media_content_id
            data["media_content_type"] = media_content_type
        self._client.call_service("media_player", service, entity_id=entity_id, **data)
        return {"player": entity_id, "action": action, **({"volume_percent": volume_percent} if action == "volume" else {})}
