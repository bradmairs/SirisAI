"""Home Assistant tools.

Deliberately generic -- domain/service/entity_id, the same shape Home
Assistant's own service-call model uses -- rather than one tool per device,
so this works for whatever's actually in a given homelab without code
changes. Registered only when SIRISAI_HOME_ASSISTANT_BASE_URL and
SIRISAI_HOME_ASSISTANT_TOKEN are both set (see app/core/siris.py); until
then SirisAI simply doesn't offer these tools to the model.
"""

from typing import Any

from app.home_assistant.client import HomeAssistantClient
from app.memory.events import EventMemoryStore
from app.plugins.base import Tool, ToolPermission


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
        return [{"entity_id": s["entity_id"], "state": s["state"]} for s in self._client.list_states(domain=domain)]


class HomeAssistantGetStateTool(Tool):
    name = "home_assistant_get_state"
    description = "Get the current state and attributes of one Home Assistant entity."
    permission = ToolPermission.READ
    parameters = {
        "type": "object",
        "properties": {
            "entity_id": {"type": "string", "description": "The entity to look up, e.g. 'light.living_room'."},
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
    # Confirm before running rather than trying to allowlist safe services.
    permission = ToolPermission.CONFIRM_REQUIRED
    parameters = {
        "type": "object",
        "properties": {
            "domain": {"type": "string", "description": "Service domain, e.g. 'light', 'switch', 'climate'."},
            "service": {
                "type": "string",
                "description": "Service to call, e.g. 'turn_on', 'turn_off', 'toggle', 'set_temperature'.",
            },
            "entity_id": {"type": "string", "description": "The entity to target, e.g. 'light.living_room'."},
            "service_data": {
                "type": "object",
                "description": "Extra service-specific fields, e.g. {'brightness_pct': 50}.",
            },
        },
        "required": ["domain", "service"],
    }

    def __init__(self, client: HomeAssistantClient, events: EventMemoryStore) -> None:
        self._client = client
        self._events = events

    def run(
        self,
        domain: str = "",
        service: str = "",
        entity_id: str | None = None,
        service_data: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Any:
        result = self._client.call_service(domain, service, entity_id=entity_id, **(service_data or {}))
        # Behavioral pattern anomalies (milestone 41): log this as an
        # Event, grouped by "{domain}.{service}" + entity_id (e.g.
        # "light.turn_off" on "light.office"), so EventMemoryStore's
        # existing timing-baseline machinery (37) can learn when this
        # normally happens and flag it when it doesn't -- "the office
        # lights are usually off by 11" is the same shape of anomaly as
        # "the backup didn't run", once the action is visible as a
        # recurring Event signature. No new detection logic needed:
        # event_timing_check already does the rest once this exists.
        self._events.log_event(f"{domain}.{service}", entity_name=entity_id, source="home_assistant_call_service")
        return result
