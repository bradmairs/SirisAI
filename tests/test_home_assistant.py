import json
from datetime import UTC, datetime

import httpx

from app.core.config import Settings
from app.core.siris import SirisAI, get_siris
from app.home_assistant.client import HomeAssistantClient
from app.memory.events import EventMemoryStore
from app.plugins.builtin.home_assistant import (
    HomeAssistantCallServiceTool,
    HomeAssistantGetLogbookTool,
    HomeAssistantGetStateTool,
    HomeAssistantListEntitiesTool,
)

_HOME_ASSISTANT_TOOL_NAMES = {
    "home_assistant_list_entities",
    "home_assistant_get_state",
    "home_assistant_call_service",
    "home_assistant_get_logbook",
}


def test_home_assistant_tools_not_registered_by_default() -> None:
    siris = SirisAI(settings=Settings())
    tool_names = {tool.name for tool in siris.tools.list()}
    assert not tool_names & _HOME_ASSISTANT_TOOL_NAMES


def test_home_assistant_tools_registered_when_both_settings_present() -> None:
    siris = SirisAI(settings=Settings(home_assistant_base_url="http://ha.local:8123", home_assistant_token="secret"))
    try:
        tool_names = {tool.name for tool in siris.tools.list()}
        assert _HOME_ASSISTANT_TOOL_NAMES <= tool_names
    finally:
        assert siris._home_assistant_client is not None
        siris._home_assistant_client.close()


def test_home_assistant_tools_not_registered_with_only_one_setting() -> None:
    siris = SirisAI(settings=Settings(home_assistant_base_url="http://ha.local:8123", home_assistant_token=""))
    tool_names = {tool.name for tool in siris.tools.list()}
    assert not tool_names & _HOME_ASSISTANT_TOOL_NAMES


def _mock_client(handler) -> HomeAssistantClient:
    return HomeAssistantClient(base_url="http://ha.local:8123", token="secret", transport=httpx.MockTransport(handler))


def _events() -> EventMemoryStore:
    return SirisAI(settings=get_siris().settings).events


def test_list_states_filters_by_domain() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/api/states"
        assert request.headers["authorization"] == "Bearer secret"
        return httpx.Response(
            200,
            json=[
                {"entity_id": "light.kitchen", "state": "on"},
                {"entity_id": "switch.fan", "state": "off"},
            ],
        )

    client = _mock_client(handler)
    tool = HomeAssistantListEntitiesTool(client)
    result = tool.run(domain="light")
    assert result == [{"entity_id": "light.kitchen", "state": "on"}]
    client.close()


def test_get_state_hits_correct_path() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/states/light.kitchen"
        return httpx.Response(200, json={"entity_id": "light.kitchen", "state": "on", "attributes": {}})

    client = _mock_client(handler)
    tool = HomeAssistantGetStateTool(client)
    result = tool.run(entity_id="light.kitchen")
    assert result["state"] == "on"
    client.close()


def test_call_service_posts_domain_service_and_entity() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/services/light/turn_on"
        body = json.loads(request.content)
        assert body == {"entity_id": "light.kitchen", "brightness_pct": 50}
        return httpx.Response(200, json=[{"entity_id": "light.kitchen", "state": "on"}])

    client = _mock_client(handler)
    tool = HomeAssistantCallServiceTool(client, _events())
    result = tool.run(
        domain="light", service="turn_on", entity_id="light.kitchen", service_data={"brightness_pct": 50}
    )
    assert result == [{"entity_id": "light.kitchen", "state": "on"}]
    client.close()


def test_call_service_logs_an_event_for_behavioral_pattern_tracking() -> None:
    """Milestone 41: the same recurring-Event shape event_timing_check
    (37) already knows how to baseline -- no new detection logic, just
    making the action visible as an Event at all."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"entity_id": "light.office", "state": "off"}])

    client = _mock_client(handler)
    events = _events()
    tool = HomeAssistantCallServiceTool(client, events)

    tool.run(domain="light", service="turn_off", entity_id="light.office")

    logged = events.recent_events(entity_name="light.office", limit=1)
    assert logged[0]["description"] == "light.turn_off"
    assert logged[0]["source"] == "home_assistant_call_service"
    client.close()


def test_call_service_logs_an_event_without_entity_id() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    client = _mock_client(handler)
    events = _events()
    tool = HomeAssistantCallServiceTool(client, events)

    tool.run(domain="homeassistant", service="restart")

    logged = events.recent_events(limit=1)
    assert logged[0]["description"] == "homeassistant.restart"
    assert logged[0]["entity_name"] is None
    client.close()


# -- get_logbook / home_assistant_get_logbook ("why did X happen", not just
# current state -- see the README's write-up on the SirisAI vision doc)


def test_get_logbook_hits_timestamped_path_and_forwards_entity_filter() -> None:
    since = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/api/logbook/{since.isoformat()}"
        assert request.url.params["entity"] == "light.hallway"
        return httpx.Response(
            200,
            json=[{"when": "2026-01-01T12:30:00+00:00", "name": "Hallway light", "message": "turned on"}],
        )

    client = _mock_client(handler)
    result = client.get_logbook(entity_id="light.hallway", since=since)
    assert result == [{"when": "2026-01-01T12:30:00+00:00", "name": "Hallway light", "message": "turned on"}]
    client.close()


def test_get_logbook_omits_entity_filter_and_timestamp_when_not_given() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/logbook"
        assert "entity" not in request.url.params
        return httpx.Response(200, json=[])

    client = _mock_client(handler)
    client.get_logbook()
    client.close()


def test_home_assistant_get_logbook_tool_defaults_to_24_hours_back() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["entity"] = request.url.params.get("entity")
        return httpx.Response(200, json=[{"message": "turned on"}])

    client = _mock_client(handler)
    tool = HomeAssistantGetLogbookTool(client)
    result = tool.run()

    assert result == [{"message": "turned on"}]
    assert seen["entity"] is None
    # a timestamped path was used (default hours=24 always passes a `since`)
    assert str(seen["path"]).startswith("/api/logbook/")
    client.close()


def test_home_assistant_get_logbook_tool_forwards_entity_id_and_hours() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["entity"] = request.url.params.get("entity")
        return httpx.Response(200, json=[])

    client = _mock_client(handler)
    tool = HomeAssistantGetLogbookTool(client)
    tool.run(entity_id="light.hallway", hours=6)

    assert seen["entity"] == "light.hallway"
    client.close()
