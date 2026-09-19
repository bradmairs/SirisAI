import json
from datetime import UTC, datetime
from unittest import mock

import httpx
import pytest

from app.core.config import Settings
from app.core.siris import SirisAI, get_siris
from app.home_assistant.client import HomeAssistantClient
from app.memory.events import EventMemoryStore
from app.plugins.base import ConfirmationRequired, ToolPermission
from app.plugins.builtin.home_assistant import (
    HomeAssistantCallServiceTool,
    HomeAssistantCreateAutomationTool,
    HomeAssistantDeleteAutomationTool,
    HomeAssistantGetHistoryTool,
    HomeAssistantGetLogbookTool,
    HomeAssistantGetStateTool,
    HomeAssistantListEntitiesTool,
    HomeAssistantRenderTemplateTool,
    HomeAssistantResolveEntityTool,
)

_HOME_ASSISTANT_TOOL_NAMES = {
    "home_assistant_list_entities",
    "home_assistant_get_state",
    "home_assistant_call_service",
    "home_assistant_get_logbook",
    "home_assistant_create_automation",
}


def test_home_assistant_tools_not_registered_by_default() -> None:
    siris = SirisAI(settings=Settings())
    tool_names = {tool.name for tool in siris.tools.list()}
    assert not tool_names & _HOME_ASSISTANT_TOOL_NAMES


def test_home_assistant_tools_registered_when_both_settings_present() -> None:
    siris = SirisAI(
        settings=Settings(
            home_assistant_base_url="http://ha.local:8123",
            home_assistant_token="secret",
        )
    )
    try:
        tool_names = {tool.name for tool in siris.tools.list()}
        assert _HOME_ASSISTANT_TOOL_NAMES <= tool_names
    finally:
        assert siris._home_assistant_client is not None
        siris._home_assistant_client.close()


def test_home_assistant_tools_not_registered_with_only_one_setting() -> None:
    siris = SirisAI(
        settings=Settings(
            home_assistant_base_url="http://ha.local:8123", home_assistant_token=""
        )
    )
    tool_names = {tool.name for tool in siris.tools.list()}
    assert not tool_names & _HOME_ASSISTANT_TOOL_NAMES


def _mock_client(handler) -> HomeAssistantClient:
    return HomeAssistantClient(
        base_url="http://ha.local:8123",
        token="secret",
        transport=httpx.MockTransport(handler),
    )


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
        return httpx.Response(
            200, json={"entity_id": "light.kitchen", "state": "on", "attributes": {}}
        )

    client = _mock_client(handler)
    tool = HomeAssistantGetStateTool(client)
    result = tool.run(entity_id="light.kitchen")
    assert result["state"] == "on"
    client.close()


def test_resolve_entity_matches_by_friendly_name() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/states"
        return httpx.Response(
            200,
            json=[
                {
                    "entity_id": "light.office_lamp_2",
                    "state": "off",
                    "attributes": {"friendly_name": "Office Lamp"},
                },
                {
                    "entity_id": "switch.fan",
                    "state": "off",
                    "attributes": {"friendly_name": "Fan"},
                },
            ],
        )

    client = _mock_client(handler)
    result = HomeAssistantResolveEntityTool(client).run(name_hint="office light")
    assert result["ambiguous"] is False
    assert result["matches"] == [
        {
            "entity_id": "light.office_lamp_2",
            "friendly_name": "Office Lamp",
            "state": "off",
        }
    ]
    client.close()


def test_resolve_entity_matches_by_entity_id_when_no_friendly_name() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json=[{"entity_id": "light.kitchen", "state": "on", "attributes": {}}]
        )

    client = _mock_client(handler)
    result = HomeAssistantResolveEntityTool(client).run(name_hint="kitchen")
    assert result["ambiguous"] is False
    assert result["matches"][0]["entity_id"] == "light.kitchen"
    assert result["matches"][0]["friendly_name"] is None
    client.close()


def test_resolve_entity_flags_ambiguous_for_multiple_matches() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {
                    "entity_id": "light.office_desk",
                    "state": "off",
                    "attributes": {"friendly_name": "Office Desk Lamp"},
                },
                {
                    "entity_id": "light.office_ceiling",
                    "state": "on",
                    "attributes": {"friendly_name": "Office Ceiling Light"},
                },
            ],
        )

    client = _mock_client(handler)
    result = HomeAssistantResolveEntityTool(client).run(name_hint="office light")
    assert result["ambiguous"] is True
    assert len(result["matches"]) == 2
    client.close()


def test_resolve_entity_forwards_domain_filter() -> None:
    """Domain filtering happens in HomeAssistantClient.list_states() itself
    (client-side, on the full /api/states response) -- this proves the
    tool actually passes `domain` through rather than ignoring it."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {"entity_id": "light.office", "state": "off", "attributes": {}},
                {"entity_id": "switch.office_fan", "state": "off", "attributes": {}},
            ],
        )

    client = _mock_client(handler)
    result = HomeAssistantResolveEntityTool(client).run(
        name_hint="office", domain="light"
    )
    assert [m["entity_id"] for m in result["matches"]] == ["light.office"]
    client.close()


# -- Fuzzy matching (a user-requested hardening pass): "a slight variation"
# of an entity's real name should still resolve, without requiring an exact
# literal match.


def test_resolve_entity_matches_a_plural_of_the_entitys_singular_name() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {
                    "entity_id": "light.lounge_lamp",
                    "state": "on",
                    "attributes": {"friendly_name": "Lounge Lamp"},
                }
            ],
        )

    client = _mock_client(handler)
    result = HomeAssistantResolveEntityTool(client).run(name_hint="lounge lamps")
    assert [m["entity_id"] for m in result["matches"]] == ["light.lounge_lamp"]
    client.close()


def test_resolve_entity_matches_a_minor_spelling_variation() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {
                    "entity_id": "light.lounge_lamp",
                    "state": "on",
                    "attributes": {"friendly_name": "Lounge Lamp"},
                }
            ],
        )

    client = _mock_client(handler)
    # "loung" -- one letter short of "lounge".
    result = HomeAssistantResolveEntityTool(client).run(name_hint="loung lamp")
    assert [m["entity_id"] for m in result["matches"]] == ["light.lounge_lamp"]
    client.close()


def test_resolve_entity_does_not_match_a_genuinely_different_word() -> None:
    """Fuzzy tolerance is spelling-closeness, not synonym understanding --
    'lamp' should not match an entity whose name is just 'Lounge Light'."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {
                    "entity_id": "switch.lounge_light",
                    "state": "on",
                    "attributes": {"friendly_name": "Lounge Light"},
                }
            ],
        )

    client = _mock_client(handler)
    result = HomeAssistantResolveEntityTool(client).run(name_hint="lounge lamp")
    assert result["matches"] == []
    client.close()


def test_resolve_entity_fuzzy_matching_still_supports_multiple_ambiguous_matches() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {
                    "entity_id": "light.lounge_lamp_1",
                    "state": "on",
                    "attributes": {"friendly_name": "Lounge Lamp 1"},
                },
                {
                    "entity_id": "light.lounge_lamp_2",
                    "state": "off",
                    "attributes": {"friendly_name": "Lounge Lamp 2"},
                },
            ],
        )

    client = _mock_client(handler)
    result = HomeAssistantResolveEntityTool(client).run(name_hint="lounge lamps")
    assert result["ambiguous"] is True
    assert len(result["matches"]) == 2
    client.close()


def test_call_service_posts_domain_service_and_entity() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            # The fuzzy-fallback existence check (run() calls this before
            # the actual service call) -- entity exists, so nothing else
            # should happen here.
            assert request.url.path == "/api/states/light.kitchen"
            return httpx.Response(200, json={"entity_id": "light.kitchen", "state": "on"})
        assert request.method == "POST"
        assert request.url.path == "/api/services/light/turn_on"
        body = json.loads(request.content)
        assert body == {"entity_id": "light.kitchen", "brightness_pct": 50}
        return httpx.Response(200, json=[{"entity_id": "light.kitchen", "state": "on"}])

    client = _mock_client(handler)
    tool = HomeAssistantCallServiceTool(client, _events())
    result = tool.run(
        domain="light",
        service="turn_on",
        entity_id="light.kitchen",
        service_data={"brightness_pct": 50},
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


# -- Fuzzy fallback (a user-requested hardening pass): the model is
# supposed to call home_assistant_resolve_entity first when it doesn't
# already know the exact entity_id, but when it guesses wrong anyway,
# this should recover rather than silently failing/no-op-ing against a
# nonexistent entity.


def _fallback_handler(states: list[dict], expected_final_entity_id: str):
    """A single mock handler wired for the full run() sequence: GET the
    guessed entity_id (404 -- it doesn't exist), GET the domain's full
    state list (to find a fuzzy candidate), then POST the service call
    against whatever entity_id run() decided to use."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/api/states":
            return httpx.Response(200, json=states)
        if request.method == "GET":
            return httpx.Response(404)
        assert request.method == "POST"
        assert request.url.path == "/api/services/light/turn_off"
        body = json.loads(request.content)
        assert body["entity_id"] == expected_final_entity_id
        return httpx.Response(200, json=[{"entity_id": expected_final_entity_id, "state": "off"}])

    return handler


def test_call_service_substitutes_a_single_fuzzy_match_for_a_nonexistent_entity() -> None:
    states = [
        {
            "entity_id": "light.lounge_lamp",
            "state": "on",
            "attributes": {"friendly_name": "Lounge Lamp"},
        }
    ]
    client = _mock_client(_fallback_handler(states, "light.lounge_lamp"))
    tool = HomeAssistantCallServiceTool(client, _events())

    result = tool.run(domain="light", service="turn_off", entity_id="light.loung_lamp")

    assert result["resolved_entity_id"] == "light.lounge_lamp"
    assert "light.loung_lamp" in result["note"]
    assert "light.lounge_lamp" in result["note"]
    assert result["result"] == [{"entity_id": "light.lounge_lamp", "state": "off"}]
    client.close()


def test_call_service_logs_the_event_against_the_resolved_entity() -> None:
    states = [
        {
            "entity_id": "light.lounge_lamp",
            "state": "on",
            "attributes": {"friendly_name": "Lounge Lamp"},
        }
    ]
    client = _mock_client(_fallback_handler(states, "light.lounge_lamp"))
    events = _events()
    tool = HomeAssistantCallServiceTool(client, events)

    tool.run(domain="light", service="turn_off", entity_id="light.loung_lamp")

    logged = events.recent_events(entity_name="light.lounge_lamp", limit=1)
    assert logged[0]["description"] == "light.turn_off"
    client.close()


def test_call_service_leaves_entity_id_untouched_when_no_fuzzy_candidate_exists() -> None:
    states = [
        {
            "entity_id": "switch.fan",
            "state": "on",
            "attributes": {"friendly_name": "Fan"},
        }
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/api/states":
            return httpx.Response(200, json=states)
        if request.method == "GET":
            return httpx.Response(404)
        body = json.loads(request.content)
        assert body["entity_id"] == "light.totally_unrelated"
        return httpx.Response(200, json=[])

    client = _mock_client(handler)
    tool = HomeAssistantCallServiceTool(client, _events())

    result = tool.run(domain="light", service="turn_off", entity_id="light.totally_unrelated")

    assert result == []
    client.close()


def test_call_service_leaves_entity_id_untouched_when_multiple_fuzzy_candidates_exist() -> None:
    states = [
        {
            "entity_id": "light.lounge_lamp_1",
            "state": "on",
            "attributes": {"friendly_name": "Lounge Lamp 1"},
        },
        {
            "entity_id": "light.lounge_lamp_2",
            "state": "off",
            "attributes": {"friendly_name": "Lounge Lamp 2"},
        },
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/api/states":
            return httpx.Response(200, json=states)
        if request.method == "GET":
            return httpx.Response(404)
        body = json.loads(request.content)
        assert body["entity_id"] == "light.lounge_lamp"
        return httpx.Response(200, json=[])

    client = _mock_client(handler)
    tool = HomeAssistantCallServiceTool(client, _events())

    result = tool.run(domain="light", service="turn_off", entity_id="light.lounge_lamp")

    assert result == []
    client.close()


def test_call_service_skips_the_existence_check_without_an_entity_id() -> None:
    """A domain-wide service call (e.g. homeassistant.restart) has no
    entity_id to check -- the fallback logic must not attempt one."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST", "no GET should happen when entity_id is None"
        return httpx.Response(200, json=[])

    client = _mock_client(handler)
    tool = HomeAssistantCallServiceTool(client, _events())

    result = tool.run(domain="homeassistant", service="restart")

    assert result == []
    client.close()


def test_verify_uses_the_resolved_entity_id_after_a_fuzzy_substitution() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/api/states/light.lounge_lamp":
            return httpx.Response(
                200, json={"entity_id": "light.lounge_lamp", "state": "off", "attributes": {}}
            )
        return httpx.Response(404)

    client = _mock_client(handler)
    tool = HomeAssistantCallServiceTool(client, _events())

    result = {"result": [], "resolved_entity_id": "light.lounge_lamp", "note": "substituted"}
    verification = tool.verify(
        {"domain": "light", "service": "turn_off", "entity_id": "light.loung_lamp"}, result
    )

    assert verification.verified is True
    assert "light.lounge_lamp" in verification.detail
    client.close()


# -- verify(): result validation (milestone 65) -- the API call returning
# success doesn't mean the entity actually reached the expected state (an
# unreachable/offline device is the common real failure mode), same gap
# DockerRestartContainerTool.verify() (38) closed for container restarts.


def test_verify_reports_success_when_entity_reaches_the_expected_state() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(
                200, json=[{"entity_id": "light.kitchen", "state": "on"}]
            )
        assert request.url.path == "/api/states/light.kitchen"
        return httpx.Response(
            200, json={"entity_id": "light.kitchen", "state": "on", "attributes": {}}
        )

    client = _mock_client(handler)
    tool = HomeAssistantCallServiceTool(client, _events())
    result = tool.run(domain="light", service="turn_on", entity_id="light.kitchen")

    verification = tool.verify(
        {"domain": "light", "service": "turn_on", "entity_id": "light.kitchen"}, result
    )
    assert verification.verified is True
    assert "on" in verification.detail
    client.close()


def test_verify_reports_failure_when_entity_never_reached_the_expected_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exact gap this exists for: an offline light can accept the
    turn_on call (HA still returns 200) without ever actually turning on."""
    monkeypatch.setattr("app.plugins.builtin.home_assistant._VERIFY_POLL_SECONDS", 0)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(
                200, json=[{"entity_id": "light.kitchen", "state": "on"}]
            )
        return httpx.Response(
            200,
            json={
                "entity_id": "light.kitchen",
                "state": "unavailable",
                "attributes": {},
            },
        )

    client = _mock_client(handler)
    tool = HomeAssistantCallServiceTool(client, _events())
    result = tool.run(domain="light", service="turn_on", entity_id="light.kitchen")

    verification = tool.verify(
        {"domain": "light", "service": "turn_on", "entity_id": "light.kitchen"}, result
    )
    assert verification.verified is False
    assert "unavailable" in verification.detail
    assert "on" in verification.detail


def test_verify_waits_for_a_slow_device_to_report_its_new_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Found live: a real light was "on" 2s after turn_on, but verify()
    read it back instantly, saw "off", and recorded a false failure."""
    monkeypatch.setattr("app.plugins.builtin.home_assistant._VERIFY_POLL_SECONDS", 0)
    reads = iter(["off", "off", "on"])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"entity_id": "light.kitchen", "state": next(reads), "attributes": {}},
        )

    client = _mock_client(handler)
    tool = HomeAssistantCallServiceTool(client, _events())

    verification = tool.verify(
        {"domain": "light", "service": "turn_on", "entity_id": "light.kitchen"}, {}
    )

    assert verification.verified is True
    assert "'on'" in verification.detail
    client.close()


def test_verify_stops_polling_as_soon_as_the_state_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.plugins.builtin.home_assistant._VERIFY_POLL_SECONDS", 0)
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(
            200, json={"entity_id": "light.kitchen", "state": "on", "attributes": {}}
        )

    client = _mock_client(handler)
    tool = HomeAssistantCallServiceTool(client, _events())
    tool.verify({"domain": "light", "service": "turn_on", "entity_id": "light.kitchen"}, {})

    assert len(calls) == 1
    client.close()


def test_verify_checks_lock_and_cover_domains_too() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"entity_id": "lock.front_door", "state": "locked", "attributes": {}},
        )

    client = _mock_client(handler)
    tool = HomeAssistantCallServiceTool(client, _events())

    verification = tool.verify(
        {"domain": "lock", "service": "lock", "entity_id": "lock.front_door"}, {}
    )
    assert verification.verified is True


def test_verify_raises_not_implemented_without_an_entity_id() -> None:
    tool = HomeAssistantCallServiceTool(
        _mock_client(lambda r: httpx.Response(200, json=[])), _events()
    )
    with pytest.raises(NotImplementedError):
        tool.verify({"domain": "homeassistant", "service": "restart"}, {})


def test_verify_raises_not_implemented_for_a_service_with_no_single_expected_state() -> (
    None
):
    """Honest about what it can't check -- e.g. set_temperature's "correct"
    end state depends on the requested temperature, and a scene activation
    has no single entity/state to check at all."""
    tool = HomeAssistantCallServiceTool(
        _mock_client(lambda r: httpx.Response(200, json=[])), _events()
    )
    with pytest.raises(NotImplementedError):
        tool.verify(
            {
                "domain": "climate",
                "service": "set_temperature",
                "entity_id": "climate.office",
            },
            {},
        )


# -- needs_confirmation(): a configurable per-domain confirmation bypass,
# a direct user request (lights/switches shouldn't need confirming every
# time) rather than removing confirmation for every domain including
# locks/alarms/covers.


def test_needs_confirmation_is_true_for_every_domain_with_no_allowlist_configured() -> (
    None
):
    tool = HomeAssistantCallServiceTool(
        _mock_client(lambda r: httpx.Response(200, json=[])), _events()
    )
    assert tool.needs_confirmation({"domain": "light", "service": "turn_on"}) is True
    assert tool.needs_confirmation({"domain": "lock", "service": "unlock"}) is True


def test_needs_confirmation_is_false_for_an_allowlisted_domain() -> None:
    tool = HomeAssistantCallServiceTool(
        _mock_client(lambda r: httpx.Response(200, json=[])),
        _events(),
        no_confirm_domains=["light", "switch"],
    )
    assert tool.needs_confirmation({"domain": "light", "service": "turn_on"}) is False
    assert tool.needs_confirmation({"domain": "switch", "service": "turn_off"}) is False


def test_needs_confirmation_is_still_true_for_a_non_allowlisted_domain() -> None:
    tool = HomeAssistantCallServiceTool(
        _mock_client(lambda r: httpx.Response(200, json=[])),
        _events(),
        no_confirm_domains=["light", "switch"],
    )
    assert tool.needs_confirmation({"domain": "lock", "service": "unlock"}) is True
    assert (
        tool.needs_confirmation(
            {"domain": "alarm_control_panel", "service": "alarm_disarm"}
        )
        is True
    )


def test_needs_confirmation_is_false_for_any_domain_with_wildcard() -> None:
    tool = HomeAssistantCallServiceTool(
        _mock_client(lambda r: httpx.Response(200, json=[])),
        _events(),
        no_confirm_domains=["*"],
    )
    assert tool.needs_confirmation({"domain": "media_player", "service": "turn_on"}) is False
    assert tool.needs_confirmation({"domain": "climate", "service": "set_temperature"}) is False
    assert tool.needs_confirmation({"domain": "scene", "service": "turn_on"}) is False


def test_needs_confirmation_is_true_for_lock_and_cover_even_with_wildcard() -> None:
    """The safety floor a wildcard allowlist can't bypass -- a direct user
    request: automate everything except a door lock or garage door (which
    Home Assistant models as a `cover` entity)."""
    tool = HomeAssistantCallServiceTool(
        _mock_client(lambda r: httpx.Response(200, json=[])),
        _events(),
        no_confirm_domains=["*"],
    )
    assert tool.needs_confirmation({"domain": "lock", "service": "unlock"}) is True
    assert tool.needs_confirmation({"domain": "cover", "service": "open_cover"}) is True


def test_needs_confirmation_is_true_for_lock_and_cover_even_if_explicitly_allowlisted() -> (
    None
):
    tool = HomeAssistantCallServiceTool(
        _mock_client(lambda r: httpx.Response(200, json=[])),
        _events(),
        no_confirm_domains=["lock", "cover"],
    )
    assert tool.needs_confirmation({"domain": "lock", "service": "unlock"}) is True
    assert tool.needs_confirmation({"domain": "cover", "service": "open_cover"}) is True


def test_no_confirm_domains_setting_parses_comma_separated_list() -> None:
    settings = Settings(home_assistant_no_confirm_domains=" light, switch ,fan")
    assert settings.home_assistant_no_confirm_domain_list() == [
        "light",
        "switch",
        "fan",
    ]


def test_no_confirm_domains_setting_defaults_to_empty() -> None:
    assert Settings().home_assistant_no_confirm_domain_list() == []


def test_allowlisted_domain_call_runs_without_explicit_confirmation_end_to_end() -> (
    None
):
    """Proves the setting actually flows through registration
    (app/core/siris.py) into the registered tool instance, not just that
    HomeAssistantCallServiceTool.needs_confirmation() works in isolation."""
    from unittest.mock import patch

    siris = SirisAI(
        settings=Settings(
            home_assistant_base_url="http://ha.local:8123",
            home_assistant_token="secret",
            home_assistant_no_confirm_domains="light",
        )
    )
    try:
        assert siris._home_assistant_client is not None
        with (
            patch.object(
                siris._home_assistant_client,
                "get_state_or_none",
                return_value={"entity_id": "light.kitchen", "state": "off"},
            ),
            patch.object(
                siris._home_assistant_client,
                "call_service",
                return_value=[{"entity_id": "light.kitchen", "state": "on"}],
            ) as mock_call,
        ):
            result = siris.run_tool(
                "home_assistant_call_service",
                {"domain": "light", "service": "turn_on", "entity_id": "light.kitchen"},
            )
        assert result == [{"entity_id": "light.kitchen", "state": "on"}]
        mock_call.assert_called_once_with("light", "turn_on", entity_id="light.kitchen")

        ran = siris.audit.list_recent(limit=1)[0]
        assert ran["confirmation_required"] is False
        assert (
            ran["policy"]
            == "CONFIRM_REQUIRED: this call's arguments are allowlisted to skip confirmation"
        )
    finally:
        siris._home_assistant_client.close()


def test_non_allowlisted_domain_still_requires_confirmation_end_to_end() -> None:
    from app.plugins.base import ConfirmationRequired

    import pytest

    siris = SirisAI(
        settings=Settings(
            home_assistant_base_url="http://ha.local:8123",
            home_assistant_token="secret",
            home_assistant_no_confirm_domains="light",
        )
    )
    try:
        with pytest.raises(ConfirmationRequired):
            siris.run_tool(
                "home_assistant_call_service",
                {"domain": "lock", "service": "unlock", "entity_id": "lock.front_door"},
            )
    finally:
        assert siris._home_assistant_client is not None
        siris._home_assistant_client.close()


# -- create_automation / home_assistant_create_automation (milestone 54)


def test_create_automation_posts_to_config_api_with_a_generated_id() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path.startswith("/api/config/automation/config/")
        automation_id = request.url.path.rsplit("/", 1)[-1]
        assert automation_id.isdigit()  # a millisecond timestamp
        body = json.loads(request.content)
        assert body == {
            "alias": "Hallway motion light",
            "trigger": [
                {
                    "platform": "state",
                    "entity_id": "binary_sensor.hallway_motion",
                    "to": "on",
                }
            ],
            "action": [
                {"service": "light.turn_on", "target": {"entity_id": "light.hallway"}}
            ],
        }
        return httpx.Response(200, json={"result": "ok"})

    client = _mock_client(handler)
    tool = HomeAssistantCreateAutomationTool(client)
    result = tool.run(
        alias="Hallway motion light",
        trigger=[
            {
                "platform": "state",
                "entity_id": "binary_sensor.hallway_motion",
                "to": "on",
            }
        ],
        action=[{"service": "light.turn_on", "target": {"entity_id": "light.hallway"}}],
    )
    assert result["result"] == "ok"
    assert result["automation_id"].isdigit()
    client.close()


def test_create_automation_includes_condition_mode_and_description_when_given() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["condition"] == [{"condition": "sun", "after": "sunset"}]
        assert body["mode"] == "restart"
        assert body["description"] == "Only after dark"
        return httpx.Response(200, json={"result": "ok"})

    client = _mock_client(handler)
    tool = HomeAssistantCreateAutomationTool(client)
    tool.run(
        alias="Hallway motion light",
        trigger=[
            {
                "platform": "state",
                "entity_id": "binary_sensor.hallway_motion",
                "to": "on",
            }
        ],
        action=[{"service": "light.turn_on", "target": {"entity_id": "light.hallway"}}],
        condition=[{"condition": "sun", "after": "sunset"}],
        mode="restart",
        description="Only after dark",
    )
    client.close()


def test_create_automation_omits_optional_fields_when_not_given() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert "condition" not in body
        assert "mode" not in body
        assert "description" not in body
        return httpx.Response(200, json={"result": "ok"})

    client = _mock_client(handler)
    tool = HomeAssistantCreateAutomationTool(client)
    tool.run(
        alias="X",
        trigger=[{"platform": "state"}],
        action=[{"service": "light.turn_on"}],
    )
    client.close()


# -- verify(): same gap as HomeAssistantCallServiceTool's (milestone 65),
# extended to automations (a hardening pass): create_automation() hits
# HA's undocumented config-editor endpoint (see the client's module
# docstring), so a 200 there doesn't prove the automation actually
# persisted.


def test_create_automation_verify_reports_success_when_automation_is_found() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/states"
        return httpx.Response(
            200,
            json=[
                {
                    "entity_id": "automation.hallway_motion_light",
                    "state": "on",
                    "attributes": {"id": "123"},
                }
            ],
        )

    client = _mock_client(handler)
    tool = HomeAssistantCreateAutomationTool(client)

    verification = tool.verify({}, {"result": "ok", "automation_id": "123"})
    assert verification.verified is True
    assert "123" in verification.detail
    client.close()


def test_create_automation_verify_reports_failure_when_automation_is_missing() -> None:
    client = _mock_client(lambda request: httpx.Response(200, json=[]))
    tool = HomeAssistantCreateAutomationTool(client)

    verification = tool.verify({}, {"result": "ok", "automation_id": "123"})
    assert verification.verified is False
    assert "123" in verification.detail
    client.close()


def test_create_automation_verify_raises_not_implemented_without_an_automation_id() -> (
    None
):
    """The result HA itself returns has no automation_id -- only
    create_automation() merges one in -- so a result missing it (e.g. a
    caller passing something else through) can't be checked."""
    tool = HomeAssistantCreateAutomationTool(
        _mock_client(lambda r: httpx.Response(200, json=[]))
    )
    with pytest.raises(NotImplementedError):
        tool.verify({}, {"result": "ok"})


def test_create_automation_tool_requires_confirmation_unconditionally() -> None:
    """Unlike home_assistant_call_service, this is a separate Tool class
    with no needs_confirmation() override -- there's no allowlist that
    can skip confirming a standing automation, regardless of
    SIRISAI_HOME_ASSISTANT_NO_CONFIRM_DOMAINS."""
    client = _mock_client(lambda request: httpx.Response(200, json={"result": "ok"}))
    tool = HomeAssistantCreateAutomationTool(client)
    assert tool.permission == ToolPermission.CONFIRM_REQUIRED
    assert tool.needs_confirmation({"alias": "X", "trigger": [], "action": []}) is True
    client.close()


def test_create_automation_tool_is_registered_and_confirm_required_end_to_end() -> None:
    siris = SirisAI(
        settings=Settings(
            home_assistant_base_url="http://ha.local:8123",
            home_assistant_token="secret",
        )
    )
    try:
        assert siris._home_assistant_client is not None
        with mock.patch.object(
            siris._home_assistant_client,
            "create_automation",
            return_value={"result": "ok", "automation_id": "123"},
        ):
            with pytest.raises(ConfirmationRequired):
                siris.run_tool(
                    "home_assistant_create_automation",
                    {
                        "alias": "X",
                        "trigger": [{"platform": "state"}],
                        "action": [{"service": "light.turn_on"}],
                    },
                )

            result = siris.run_tool(
                "home_assistant_create_automation",
                {
                    "alias": "X",
                    "trigger": [{"platform": "state"}],
                    "action": [{"service": "light.turn_on"}],
                },
                confirmed=True,
            )
            assert result["automation_id"] == "123"
    finally:
        siris._home_assistant_client.close()


# -- get_logbook / home_assistant_get_logbook ("why did X happen", not just
# current state -- see the README's write-up on the SirisAI vision doc)


def test_get_logbook_hits_timestamped_path_and_forwards_entity_filter() -> None:
    since = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/api/logbook/{since.isoformat()}"
        assert request.url.params["entity"] == "light.hallway"
        return httpx.Response(
            200,
            json=[
                {
                    "when": "2026-01-01T12:30:00+00:00",
                    "name": "Hallway light",
                    "message": "turned on",
                }
            ],
        )

    client = _mock_client(handler)
    result = client.get_logbook(entity_id="light.hallway", since=since)
    assert result == [
        {
            "when": "2026-01-01T12:30:00+00:00",
            "name": "Hallway light",
            "message": "turned on",
        }
    ]
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


# -- get_history / home_assistant_get_history


def test_get_history_hits_timestamped_path_and_maps_state_series() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["entity"] = request.url.params.get("filter_entity_id")
        return httpx.Response(
            200,
            json=[
                [
                    {"state": "18.0", "last_changed": "2026-01-01T00:00:00+00:00"},
                    {"state": "19.5", "last_changed": "2026-01-01T06:00:00+00:00"},
                ]
            ],
        )

    client = _mock_client(handler)
    result = HomeAssistantGetHistoryTool(client).run(
        entity_id="sensor.living_room_temperature", hours=12
    )

    assert result == [
        {"state": "18.0", "last_changed": "2026-01-01T00:00:00+00:00"},
        {"state": "19.5", "last_changed": "2026-01-01T06:00:00+00:00"},
    ]
    assert seen["entity"] == "sensor.living_room_temperature"
    assert str(seen["path"]).startswith("/api/history/period/")
    client.close()


def test_get_history_returns_empty_list_when_no_series_present() -> None:
    client = _mock_client(lambda request: httpx.Response(200, json=[]))
    result = HomeAssistantGetHistoryTool(client).run(entity_id="sensor.unknown")
    assert result == []
    client.close()


# -- render_template / home_assistant_render_template


def test_render_template_posts_template_and_returns_rendered_text() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/template"
        body = json.loads(request.content)
        assert body == {"template": "{{ states('sensor.living_room_temperature') }}"}
        return httpx.Response(200, text="19.5")

    client = _mock_client(handler)
    result = HomeAssistantRenderTemplateTool(client).run(
        template="{{ states('sensor.living_room_temperature') }}"
    )
    assert result == {"rendered": "19.5"}
    client.close()


# -- delete_automation / home_assistant_delete_automation


def test_delete_automation_sends_delete_to_config_api() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert request.url.path == "/api/config/automation/config/1735689000000"
        return httpx.Response(200, json={})

    client = _mock_client(handler)
    result = HomeAssistantDeleteAutomationTool(client).run(
        automation_id="1735689000000"
    )
    assert result == {"deleted": True, "automation_id": "1735689000000"}
    client.close()


def test_delete_automation_requires_confirmation() -> None:
    assert (
        HomeAssistantDeleteAutomationTool.permission is ToolPermission.CONFIRM_REQUIRED
    )


# -- verify(): mirror of HomeAssistantCreateAutomationTool's -- a 200 from
# the DELETE endpoint doesn't prove the automation is actually gone.


def test_delete_automation_verify_reports_success_when_automation_is_gone() -> None:
    client = _mock_client(lambda request: httpx.Response(200, json=[]))
    tool = HomeAssistantDeleteAutomationTool(client)

    verification = tool.verify(
        {"automation_id": "123"}, {"deleted": True, "automation_id": "123"}
    )
    assert verification.verified is True
    assert "123" in verification.detail
    client.close()


def test_delete_automation_verify_reports_failure_when_automation_still_present() -> (
    None
):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {
                    "entity_id": "automation.hallway_motion_light",
                    "state": "on",
                    "attributes": {"id": "123"},
                }
            ],
        )

    client = _mock_client(handler)
    tool = HomeAssistantDeleteAutomationTool(client)

    verification = tool.verify(
        {"automation_id": "123"}, {"deleted": True, "automation_id": "123"}
    )
    assert verification.verified is False
    assert "123" in verification.detail
    client.close()


def test_delete_automation_verify_raises_not_implemented_without_an_automation_id() -> (
    None
):
    tool = HomeAssistantDeleteAutomationTool(
        _mock_client(lambda r: httpx.Response(200, json=[]))
    )
    with pytest.raises(NotImplementedError):
        tool.verify({}, {"deleted": True})
