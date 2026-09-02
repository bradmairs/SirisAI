import json

import httpx

from app.core.config import Settings
from app.core.siris import SirisAI
from app.home_assistant.client import HomeAssistantClient
from app.plugins.builtin.home_assistant import (
    HomeAssistantCallServiceTool,
    HomeAssistantGetStateTool,
    HomeAssistantListEntitiesTool,
)

_HOME_ASSISTANT_TOOL_NAMES = {
    "home_assistant_list_entities",
    "home_assistant_get_state",
    "home_assistant_call_service",
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
    tool = HomeAssistantCallServiceTool(client)
    result = tool.run(
        domain="light", service="turn_on", entity_id="light.kitchen", service_data={"brightness_pct": 50}
    )
    assert result == [{"entity_id": "light.kitchen", "state": "on"}]
    client.close()
