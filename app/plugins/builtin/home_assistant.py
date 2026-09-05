"""Home Assistant tools.

Deliberately generic -- domain/service/entity_id, the same shape Home
Assistant's own service-call model uses -- rather than one tool per device,
so this works for whatever's actually in a given homelab without code
changes. Registered only when SIRISAI_HOME_ASSISTANT_BASE_URL and
SIRISAI_HOME_ASSISTANT_TOKEN are both set (see app/core/siris.py); until
then SirisAI simply doesn't offer these tools to the model.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

from app.home_assistant.client import HomeAssistantClient
from app.memory.events import EventMemoryStore
from app.plugins.base import Tool, ToolPermission, ToolVerification

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
            {"entity_id": s["entity_id"], "state": s["state"]}
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


class HomeAssistantCallServiceTool(Tool):
    name = "home_assistant_call_service"
    description = (
        "Call any Home Assistant service to control a device -- e.g. domain='light', "
        "service='turn_on', entity_id='light.living_room'. Use home_assistant_list_entities "
        "first if you don't know the entity_id. Extra service-specific fields (like "
        "'brightness_pct' or 'temperature') go in service_data."
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
        it, same as before this existed."""
        return arguments.get("domain") not in self._no_confirm_domains

    def run(
        self,
        domain: str = "",
        service: str = "",
        entity_id: str | None = None,
        service_data: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Any:
        result = self._client.call_service(
            domain, service, entity_id=entity_id, **(service_data or {})
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
            entity_name=entity_id,
            source="home_assistant_call_service",
        )
        return result

    def verify(self, arguments: dict[str, Any], result: Any) -> ToolVerification:
        """Independent check that the call actually took effect, not just
        that Home Assistant's API accepted it -- an unreachable/offline
        device is the common real failure mode this exists for: the
        service call can return success while the entity's state never
        actually changes. Only checked when both a concrete `entity_id`
        and a service in `_EXPECTED_STATE_BY_SERVICE` are given; anything
        else honestly raises `NotImplementedError` (this tool's base
        class's own "doesn't support verify" convention) rather than
        claiming to check something with no single correct end state."""
        entity_id = arguments.get("entity_id")
        expected_state = _EXPECTED_STATE_BY_SERVICE.get(arguments.get("service", ""))
        if entity_id is None or expected_state is None:
            raise NotImplementedError(
                f"{self.name} does not support verify for this domain/service"
            )

        actual_state = self._client.get_state(entity_id).get("state")
        if actual_state == expected_state:
            return ToolVerification(
                verified=True,
                detail=f"'{entity_id}' is now '{actual_state}', as expected.",
            )
        return ToolVerification(
            verified=False,
            detail=f"'{entity_id}' is '{actual_state}', not the expected '{expected_state}'.",
        )


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
                "[{'service': 'light.turn_on', 'target': {'entity_id': 'light.hallway'}}].",
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
            action or [],
            condition=condition,
            mode=mode,
            description=description,
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
