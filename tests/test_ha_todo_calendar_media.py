import json
from typing import Any

import httpx
import pytest

from app.core.config import Settings
from app.core.siris import SirisAI
from app.home_assistant.client import HomeAssistantClient
from app.plugins.base import ToolPermission
from app.plugins.builtin.home_assistant import (
    HomeAssistantAddTodoItemTool,
    HomeAssistantCompleteTodoItemTool,
    HomeAssistantGetCalendarEventsTool,
    HomeAssistantGetTodoItemsTool,
    HomeAssistantListMediaPlayersTool,
    HomeAssistantMediaControlTool,
)


def _client(handler: Any) -> HomeAssistantClient:
    return HomeAssistantClient(base_url="http://ha.local:8123", token="t", transport=httpx.MockTransport(handler))


_TODO_LISTS = [
    {"entity_id": "todo.shopping_list", "state": "2", "attributes": {"friendly_name": "Shopping List"}},
    {"entity_id": "todo.chores", "state": "1", "attributes": {"friendly_name": "Chores"}},
]


def test_tools_register_with_home_assistant_and_have_sensible_permissions() -> None:
    siris = SirisAI(settings=Settings(home_assistant_base_url="http://ha.local:8123", home_assistant_token="t"))
    perms = {t.name: t.permission for t in siris.tools.list()}
    assert perms["home_assistant_get_todo_items"] is ToolPermission.READ
    assert perms["home_assistant_get_calendar_events"] is ToolPermission.READ
    assert perms["home_assistant_list_media_players"] is ToolPermission.READ
    assert perms["home_assistant_add_todo_item"] is ToolPermission.SAFE_ACTION
    assert perms["home_assistant_media_control"] is ToolPermission.SAFE_ACTION
    siris._home_assistant_client.close()  # type: ignore[union-attr]


# -- to-do


def test_get_todo_items_reads_open_items_from_every_list() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/states":
            return httpx.Response(200, json=_TODO_LISTS)
        assert request.url.path == "/api/services/todo/get_items" and request.url.params["return_response"] == "true"
        body = json.loads(request.content)
        assert body["status"] == "needs_action"
        eid = body["entity_id"]
        return httpx.Response(
            200,
            json={"changed_states": [], "service_response": {eid: {"items": [{"summary": f"item on {eid}", "status": "needs_action", "uid": "1"}]}}},
        )

    result = HomeAssistantGetTodoItemsTool(_client(handler)).run()
    assert [r["list"] for r in result] == ["todo.shopping_list", "todo.chores"]
    assert result[0]["items"][0]["summary"] == "item on todo.shopping_list"


def test_a_named_list_is_matched_loosely() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/states":
            return httpx.Response(200, json=_TODO_LISTS)
        seen.append(json.loads(request.content)["entity_id"])
        return httpx.Response(200, json={"service_response": {seen[-1]: {"items": []}}})

    HomeAssistantGetTodoItemsTool(_client(handler)).run(list_name="shopping")
    assert seen == ["todo.shopping_list"]


def test_adding_without_a_list_when_there_are_several_asks_which() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_TODO_LISTS)

    with pytest.raises(ValueError, match="Which to-do list.*todo.shopping_list.*todo.chores"):
        HomeAssistantAddTodoItemTool(_client(handler)).run(item="milk")


def test_add_and_complete_call_the_todo_services() -> None:
    calls: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/states":
            return httpx.Response(200, json=[_TODO_LISTS[0]])
        calls.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json=[])

    client = _client(handler)
    assert HomeAssistantAddTodoItemTool(client).run(item="milk") == {"added": "milk", "list": "todo.shopping_list"}
    HomeAssistantCompleteTodoItemTool(client).run(item="milk")
    assert calls[0] == ("/api/services/todo/add_item", {"entity_id": "todo.shopping_list", "item": "milk"})
    assert calls[1][0] == "/api/services/todo/update_item" and calls[1][1]["status"] == "completed"


def test_no_todo_lists_gives_a_clear_error() -> None:
    with pytest.raises(ValueError, match="no to-do list"):
        HomeAssistantAddTodoItemTool(_client(lambda r: httpx.Response(200, json=[]))).run(item="x")


# -- calendar


def test_calendar_events_are_merged_across_calendars_and_sorted() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/calendars":
            return httpx.Response(200, json=[{"entity_id": "calendar.work", "name": "Work"}, {"entity_id": "calendar.home", "name": "Home"}])
        assert "start" in request.url.params and "end" in request.url.params
        if request.url.path.endswith("calendar.work"):
            return httpx.Response(200, json=[{"summary": "Standup", "start": {"dateTime": "2026-09-26T09:30:00+10:00"}, "end": {"dateTime": "2026-09-26T09:45:00+10:00"}}])
        return httpx.Response(200, json=[{"summary": "Bins out", "start": {"date": "2026-09-26"}, "end": {"date": "2026-09-27"}, "location": "Kerb"}])

    events = HomeAssistantGetCalendarEventsTool(_client(handler), timezone="Australia/Melbourne").run(days=2)
    assert [e["summary"] for e in events] == ["Bins out", "Standup"]
    assert events[0]["all_day"] is True and events[0]["location"] == "Kerb"
    assert events[0]["when"] == "Sat 26 Sep (all day)" and events[1]["when"] == "Sat 26 Sep 09:30"
    assert events[1]["calendar"] == "Work" and events[1]["all_day"] is False


# -- media


_PLAYERS = [
    {"entity_id": "media_player.lounge_echo", "state": "idle", "attributes": {"friendly_name": "Lounge Echo Dot", "volume_level": 0.4}},
    {"entity_id": "media_player.office_tv", "state": "playing", "attributes": {"friendly_name": "Office TV", "media_title": "The Bear", "source": "Plex"}},
]


def test_list_media_players_summarises_state_volume_and_title() -> None:
    result = HomeAssistantListMediaPlayersTool(_client(lambda r: httpx.Response(200, json=_PLAYERS))).run()
    assert result[0]["volume_percent"] == 40 and result[1]["playing"] == "The Bear" and result[1]["source"] == "Plex"


def test_media_control_resolves_a_loose_name_and_sets_volume() -> None:
    calls: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/states":
            return httpx.Response(200, json=_PLAYERS)
        calls.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json=[])

    result = HomeAssistantMediaControlTool(_client(handler)).run(player="lounge echo", action="volume", volume_percent=25)
    assert calls == [("/api/services/media_player/volume_set", {"volume_level": 0.25, "entity_id": "media_player.lounge_echo"})]
    assert result["volume_percent"] == 25


def test_media_control_validation() -> None:
    tool = HomeAssistantMediaControlTool(_client(lambda r: httpx.Response(200, json=_PLAYERS)))
    with pytest.raises(ValueError, match="Unknown action"):
        tool.run(player="office tv", action="explode")
    with pytest.raises(ValueError, match="volume_percent"):
        tool.run(player="office tv", action="volume")
    with pytest.raises(ValueError, match="media_content_id"):
        tool.run(player="office tv", action="play_media")
    with pytest.raises(ValueError, match="Which media player"):
        tool.run(player="", action="pause")  # two players: ambiguous
