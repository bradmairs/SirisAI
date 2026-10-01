"""Protocols (app/protocols/service.py), guest mode (app/core/guest.py) and
intruder response (app/skills/builtin/intruder.py)."""

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from fastapi.testclient import TestClient

from app.core import guest
from app.core.config import Settings
from app.core.siris import SirisAI
from app.frigate.client import FrigateClient
from app.home_assistant.client import HomeAssistantClient
from app.plugins.base import ConfirmationRequired
from app.protocols import service
from app.skills.builtin import intruder as intruder_mod
from app.voice.confirmations import voice_confirmable


class FakeHA:
    """Just enough Home Assistant: states, and services that change them."""

    def __init__(self) -> None:
        self.states: dict[str, dict[str, Any]] = {
            "lock.front_door": {"state": "unlocked", "attributes": {}},
            "lock.back_door": {"state": "locked", "attributes": {}},
            "cover.garage_door": {"state": "open", "attributes": {"device_class": "garage"}},
            "cover.bedroom_blind": {"state": "open", "attributes": {"device_class": "blind"}},
            "light.kitchen": {"state": "on", "attributes": {}},
            "light.hall": {"state": "off", "attributes": {}},
            "switch.guest_wifi": {"state": "off", "attributes": {}},
        }
        self.calls: list[tuple[str, str, Any]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/states":
            return httpx.Response(200, json=[{"entity_id": k, **v} for k, v in self.states.items()])
        if path.startswith("/api/states/"):
            entity = path.rsplit("/", 1)[-1]
            return httpx.Response(200, json={"entity_id": entity, **self.states[entity]}) if entity in self.states else httpx.Response(404)
        if path.startswith("/api/services/"):
            _, _, _, domain, svc = path.split("/")
            body = json.loads(request.content or b"{}")
            entity = body.get("entity_id")
            self.calls.append((domain, svc, entity))
            new = {"turn_on": "on", "turn_off": "off", "lock": "locked", "unlock": "unlocked", "open_cover": "open", "close_cover": "closed"}[svc]
            targets = [e for e in self.states if e.startswith(domain + ".")] if entity == "all" else entity if isinstance(entity, list) else [entity]
            for target in targets:
                self.states[target]["state"] = new
            return httpx.Response(200, json=[])
        return httpx.Response(404)


def _siris(ha: FakeHA, **settings: Any) -> SirisAI:
    siris = SirisAI(settings=Settings(home_assistant_base_url="http://ha", home_assistant_token="t", **settings))
    client = HomeAssistantClient(base_url="http://ha", token="t", transport=httpx.MockTransport(ha.handler))
    siris._home_assistant_client = client
    for tool in siris.tools.list():
        if hasattr(tool, "_client") and isinstance(getattr(tool, "_client"), HomeAssistantClient):
            tool._client = client  # type: ignore[attr-defined]
    return siris


@pytest.fixture(autouse=True)
def _clean_state(client: TestClient) -> Any:
    siris = SirisAI(settings=Settings())
    for key in (service.LAST_RUN_KEY, guest.STATE_KEY, intruder_mod.CURSOR_KEY, intruder_mod.LAST_ALERT_KEY):
        siris.state.delete(key)
    for proto in siris.protocols.list():
        siris.protocols.delete(proto["name"])
    siris.events.log_event("presence_changed", entity_name="presence", data={"home": True})
    yield


def _set_home(siris: SirisAI, home: bool) -> None:
    siris.events.log_event("presence_changed", entity_name="presence", data={"home": home})


def test_builtin_protocols_adapt_to_what_is_configured() -> None:
    bare = SirisAI(settings=Settings())
    # No Home Assistant, no notify: lockdown/away have no steps left; guest keeps set_guest_mode.
    assert set(service.builtin_protocols(bare)) == {"guest", "guest_off"}
    siris = _siris(FakeHA(), guest_wifi_entity_id="switch.guest_wifi")
    protocols = service.builtin_protocols(siris)
    assert {"lockdown", "away", "guest", "guest_off"} <= set(protocols)
    assert [s["tool"] for s in protocols["guest"]["steps"]] == ["set_guest_mode", "home_assistant_call_service"]


def test_run_protocol_needs_one_confirmation_and_dry_run_explains_each_step() -> None:
    siris = _siris(FakeHA())
    with pytest.raises(ConfirmationRequired):
        siris.run_tool("run_protocol", {"name": "lockdown"})
    preview = siris.tools.get("run_protocol").dry_run(name="lockdown")  # type: ignore[union-attr]
    garage = next(s for s in preview["steps"] if s["step"].startswith("Close garage"))
    assert garage["arguments"]["entity_id"] == ["cover.garage_door"]  # the blind isn't a garage
    lights = next(s for s in preview["steps"] if "lights" in s["step"].lower())
    assert lights["because"] in ("it's night", "it's daytime")


def test_lockdown_secures_the_house_and_undo_puts_it_back() -> None:
    ha = FakeHA()
    siris = _siris(ha)
    with patch("app.protocols.service.clock.now_local", return_value=datetime(2026, 9, 30, 23, 30)):
        result = siris.run_tool("run_protocol", {"name": "lockdown"}, confirmed=True)

    assert [s["status"] for s in result["steps"]] == ["ok", "ok", "ok"]
    assert ha.states["lock.front_door"]["state"] == "locked"
    assert ha.states["cover.garage_door"]["state"] == "closed"
    assert ha.states["cover.bedroom_blind"]["state"] == "open"
    assert ha.states["light.hall"]["state"] == "on"  # night: deterrent lights
    assert result["undo_available"]

    undo = siris.run_tool("undo_last_protocol", {}, confirmed=True)
    assert undo["undone"] is True
    assert ha.states["lock.front_door"]["state"] == "unlocked"
    assert ha.states["lock.back_door"]["state"] == "locked"  # was already locked: untouched
    assert ha.states["cover.garage_door"]["state"] == "open"
    assert ha.states["light.hall"]["state"] == "off"
    assert ha.states["light.kitchen"]["state"] == "on"
    assert siris.run_tool("undo_last_protocol", {}, confirmed=True) == {"undone": False, "reason": "Nothing to undo."}


def test_daytime_lockdown_skips_the_night_only_step() -> None:
    ha = FakeHA()
    siris = _siris(ha)
    with patch("app.protocols.service.clock.now_local", return_value=datetime(2026, 9, 30, 13, 0)):
        result = siris.run_tool("run_protocol", {"name": "lockdown"}, confirmed=True)
    skipped = [s for s in result["steps"] if s["status"] == "skipped"]
    assert skipped == [{"step": "All lights on (deterrent)", "status": "skipped", "reason": "it's daytime"}]
    assert ha.states["light.hall"]["state"] == "off"


def test_custom_protocol_with_conditions_and_create_validation() -> None:
    ha = FakeHA()
    siris = _siris(ha)
    create = siris.tools.get("create_protocol")
    with pytest.raises(ValueError):
        create.run(name="Bad Name!", steps=[{"tool": "echo"}])  # type: ignore[union-attr]
    with pytest.raises(ValueError):
        create.run(name="nope", steps=[{"tool": "not_a_tool"}])  # type: ignore[union-attr]
    with pytest.raises(ValueError):
        create.run(name="loop", steps=[{"tool": "run_protocol", "arguments": {"name": "loop"}}])  # type: ignore[union-attr]
    create.run(  # type: ignore[union-attr]
        name="movie_night",
        description="Dim for a film",
        steps=[
            {"tool": "home_assistant_call_service", "arguments": {"domain": "light", "service": "turn_off", "entity_id": "light.kitchen"}, "label": "Kitchen off"},
            {"tool": "home_assistant_call_service", "arguments": {"domain": "light", "service": "turn_on", "entity_id": "light.hall"}, "when": {"entity": "cover.bedroom_blind", "state": "closed"}, "label": "Hall on if blind closed"},
        ],
    )
    names = [p["name"] for p in siris.tools.get("list_protocols").run()]  # type: ignore[union-attr]
    assert "movie_night" in names
    result = service.run(siris, "movie night", "test")
    assert [s["status"] for s in result["steps"]] == ["ok", "skipped"]
    assert ha.states["light.kitchen"]["state"] == "off"
    assert siris.tools.get("delete_protocol").run(name="movie_night") == {"name": "movie_night", "deleted": True}  # type: ignore[union-attr]


def test_away_protocol_steps_conditioned_on_presence() -> None:
    ha = FakeHA()
    siris = _siris(ha)
    siris.protocols.save("leave", "", [{"tool": "home_assistant_call_service", "arguments": {"domain": "light", "service": "turn_off", "entity_id": "all"}, "when": "nobody_home"}])
    _set_home(siris, True)
    assert service.run(siris, "leave", "t")["steps"][0]["status"] == "skipped"
    _set_home(siris, False)
    assert service.run(siris, "leave", "t")["steps"][0]["status"] == "ok"
    assert ha.states["light.kitchen"]["state"] == "off"


# -- guest mode


def test_guest_protocol_hides_personal_tools_and_context_then_guest_off_restores() -> None:
    ha = FakeHA()
    siris = _siris(ha, guest_wifi_entity_id="switch.guest_wifi")
    siris.memory.remember("The spare key is under the blue pot")
    service.run(siris, "guest", "t")

    assert guest.is_on(siris)
    assert ha.states["switch.guest_wifi"]["state"] == "on"
    offered = {s["function"]["name"] for s in siris.available_tool_specs("anything")}
    assert "recall" not in offered and "remember" not in offered and "set_reminder" not in offered
    assert "home_assistant_call_service" in offered
    context = siris.context.build(siris, "where is the spare key") or ""
    assert "Guest mode is on" in context and "blue pot" not in context

    service.run(siris, "guest_off", "t")
    assert not guest.is_on(siris)
    assert ha.states["switch.guest_wifi"]["state"] == "off"
    assert "recall" in {s["function"]["name"] for s in siris.available_tool_specs("anything")}


def test_guest_mode_expires_by_itself() -> None:
    siris = SirisAI(settings=Settings())
    start = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
    guest.set_mode(siris, True, hours=2, now=start)
    assert guest.status(siris, now=start + timedelta(hours=1))["on"] is True
    assert guest.status(siris, now=start + timedelta(hours=3))["on"] is False


def test_undo_of_guest_protocol_restores_the_previous_mode() -> None:
    siris = _siris(FakeHA())
    service.run(siris, "guest", "t")
    service.undo_last(siris, "t")
    assert not guest.is_on(siris)


# -- voice confirmation of protocols


def test_protocols_with_unlocking_steps_are_not_voice_confirmable() -> None:
    siris = _siris(FakeHA())
    blocked = {"lock", "cover", "alarm_control_panel"}
    assert service.voice_confirmable(siris, "lockdown", blocked)  # only locks and closes
    siris.protocols.save("welcome", "", [{"tool": "home_assistant_call_service", "arguments": {"domain": "lock", "service": "unlock", "entity_id": "lock.front_door"}}])
    assert not service.voice_confirmable(siris, "welcome", blocked)
    assert voice_confirmable("home_assistant_call_service", {"domain": "cover", "service": "close_cover", "entity_id": "cover.garage_door"}, blocked)
    assert not voice_confirmable("home_assistant_call_service", {"domain": "cover", "service": "open_cover", "entity_id": "cover.garage_door"}, blocked)


# -- intruder response


def _frigate(events: list[dict[str, Any]]) -> FrigateClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=events)

    return FrigateClient(base_url="http://frigate", transport=httpx.MockTransport(handler))


def _event(event_id: str, minutes_ago: float, camera: str = "front_door") -> dict[str, Any]:
    start = datetime.now(UTC) - timedelta(minutes=minutes_ago)
    return {"id": event_id, "camera": camera, "label": "person", "start_time": start.timestamp(), "has_snapshot": True}


class _Pushes:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    def send(self, message: str, title: str | None = None, actions: Any = None, priority: str | None = None) -> None:
        self.sent.append({"message": message, "title": title, "priority": priority})

    def close(self) -> None:
        pass


def _intruder_siris(ha: FakeHA, events: list[dict[str, Any]], **settings: Any) -> tuple[SirisAI, _Pushes]:
    siris = _siris(ha, frigate_base_url="http://frigate", ntfy_topic="t", **settings)
    siris._frigate_client = _frigate(events)
    pushes = _Pushes()
    siris.tools.get("notify")._client = pushes  # type: ignore[union-attr]
    return siris, pushes


def test_intruder_check_baselines_then_alerts_when_nobody_is_home() -> None:
    events = [_event("old", 30)]
    siris, pushes = _intruder_siris(FakeHA(), events)
    check = siris.tools.get("intruder_check")
    assert check is not None
    assert check.run()["baseline"] is True  # type: ignore[union-attr]

    _set_home(siris, False)
    events.insert(0, _event("new", 0.5))
    with patch("app.protocols.service.clock.now_local", return_value=datetime(2026, 9, 30, 14, 0)):
        result = check.run()  # type: ignore[union-attr]
    assert [a["event_id"] for a in result["alerts"]] == ["new"]
    assert pushes.sent[0]["priority"] == "urgent"
    assert "front door camera while nobody is home" in pushes.sent[0]["message"]

    # Same camera again within the cooldown: no second alert.
    events.insert(0, _event("newer", 0.1))
    with patch("app.protocols.service.clock.now_local", return_value=datetime(2026, 9, 30, 14, 1)):
        assert check.run()["alerts"] == []  # type: ignore[union-attr]


def test_intruder_check_is_quiet_by_day_with_people_home() -> None:
    events = [_event("old", 30)]
    siris, pushes = _intruder_siris(FakeHA(), events)
    check = siris.tools.get("intruder_check")
    check.run()  # type: ignore[union-attr]
    _set_home(siris, True)
    events.insert(0, _event("new", 0.5))
    with patch("app.protocols.service.clock.now_local", return_value=datetime(2026, 9, 30, 14, 0)):
        result = check.run()  # type: ignore[union-attr]
    assert result["alerts"] == [] and pushes.sent == []


def test_intruder_auto_lockdown_when_away() -> None:
    ha = FakeHA()
    events = [_event("old", 30)]
    siris, _ = _intruder_siris(ha, events, intruder_auto_lockdown=True)
    check = siris.tools.get("intruder_check")
    check.run()  # type: ignore[union-attr]
    _set_home(siris, False)
    events.insert(0, _event("new", 0.2))
    with patch("app.protocols.service.clock.now_local", return_value=datetime(2026, 9, 30, 14, 0)):
        result = check.run()  # type: ignore[union-attr]
    assert "lockdown" in result["alerts"][0]["actions"]
    assert ha.states["lock.front_door"]["state"] == "locked"


def test_entity_id_all_is_never_fuzzy_resolved_to_one_entity() -> None:
    """Regression: 'all' used to fuzzy-match light.hall ('all' is in 'hall'),
    so 'turn off all the lights' turned off only the hall."""
    ha = FakeHA()
    ha.states["light.hall"]["state"] = "on"
    siris = _siris(ha, home_assistant_no_confirm_domains="light")
    siris.run_tool("home_assistant_call_service", {"domain": "light", "service": "turn_off", "entity_id": "all"})
    assert ("light", "turn_off", "all") in ha.calls
    assert ha.states["light.kitchen"]["state"] == "off" and ha.states["light.hall"]["state"] == "off"
