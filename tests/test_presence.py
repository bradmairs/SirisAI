from typing import Any

from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.siris import SirisAI, get_siris
from app.main import app
from app.skills.builtin.presence import PresenceSkill


class _FakeTools:
    def __init__(self, has_call_service: bool = True) -> None:
        self._has_call_service = has_call_service

    def get(self, name: str) -> object | None:
        if name == "presence_check":
            return object()  # the route checks this before calling run_tool()
        return object() if name == "home_assistant_call_service" and self._has_call_service else None


class _FakeEvents:
    def __init__(self) -> None:
        self.logged: list[dict[str, Any]] = []

    def recent_events(self, entity_name: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        matching = [e for e in reversed(self.logged) if entity_name is None or e["entity_name"] == entity_name]
        return matching[:limit]

    def log_event(
        self, description: str, entity_name: str | None = None, source: str | None = None, data: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        entry = {"description": description, "entity_name": entity_name, "source": source, "data": data}
        self.logged.append(entry)
        return entry


class _FakeSettings:
    def __init__(self, device_names: list[str], home_scene: str = "", away_scene: str = "") -> None:
        self._device_names = device_names
        self.presence_home_scene = home_scene
        self.presence_away_scene = away_scene

    def presence_device_name_list(self) -> list[str]:
        return self._device_names


class _FakeSirisForPresence:
    def __init__(
        self,
        clients: list[dict[str, Any]],
        device_names: list[str],
        home_scene: str = "",
        away_scene: str = "",
        has_call_service: bool = True,
    ) -> None:
        self.tools = _FakeTools(has_call_service)
        self.events = _FakeEvents()
        self.settings = _FakeSettings(device_names, home_scene, away_scene)
        self._clients = clients
        self.run_tool_calls: list[tuple[str, dict[str, Any]]] = []

    def run_tool(self, name: str, arguments: dict[str, Any], **kwargs: Any) -> Any:
        self.run_tool_calls.append((name, arguments))
        if name == "unifi_list_clients":
            return self._clients
        if name == "home_assistant_call_service":
            return [{"entity_id": arguments["entity_id"], "state": "activated"}]
        if name == "presence_check":
            # POST /siris/proactive/presence calls run_tool("presence_check", ...)
            # the same way it would for a real registered skill -- route it
            # through the actual skill logic (already covered by its own
            # direct tests above) rather than re-faking its composition here.
            return PresenceSkill().run(self)
        raise AssertionError(f"unexpected tool call: {name}")


_HOME_CLIENTS = [{"name": "brads-iphone", "ip_address": "192.168.1.50", "connected_at": "now"}]
_AWAY_CLIENTS = [{"name": "some-guest-device", "ip_address": "192.168.1.99", "connected_at": "now"}]


# -- PresenceSkill


def test_first_check_establishes_baseline_without_activating_a_scene() -> None:
    siris = _FakeSirisForPresence(_HOME_CLIENTS, ["brads-iphone"], home_scene="scene.welcome_home")

    result = PresenceSkill().run(siris)

    assert result == {"currently_home": True, "changed": False, "scene_activated": None}
    assert siris.events.logged[0]["data"] == {"home": True}
    assert not any(name == "home_assistant_call_service" for name, _ in siris.run_tool_calls)


def test_transition_to_away_activates_away_scene() -> None:
    siris = _FakeSirisForPresence(_AWAY_CLIENTS, ["brads-iphone"], away_scene="scene.away")
    siris.events.log_event("presence_changed", entity_name="presence", data={"home": True})

    result = PresenceSkill().run(siris)

    assert result == {"currently_home": False, "changed": True, "scene_activated": "scene.away"}
    call = next(a for name, a in siris.run_tool_calls if name == "home_assistant_call_service")
    assert call == {"domain": "scene", "service": "turn_on", "entity_id": "scene.away"}


def test_transition_to_home_activates_home_scene() -> None:
    siris = _FakeSirisForPresence(_HOME_CLIENTS, ["brads-iphone"], home_scene="scene.welcome_home")
    siris.events.log_event("presence_changed", entity_name="presence", data={"home": False})

    result = PresenceSkill().run(siris)

    assert result == {"currently_home": True, "changed": True, "scene_activated": "scene.welcome_home"}


def test_no_change_does_not_call_scene_or_log_a_new_event() -> None:
    siris = _FakeSirisForPresence(_HOME_CLIENTS, ["brads-iphone"], home_scene="scene.welcome_home")
    siris.events.log_event("presence_changed", entity_name="presence", data={"home": True})

    result = PresenceSkill().run(siris)

    assert result == {"currently_home": True, "changed": False, "scene_activated": None}
    assert len(siris.events.logged) == 1  # no new event appended
    assert not any(name == "home_assistant_call_service" for name, _ in siris.run_tool_calls)


def test_transition_without_a_configured_scene_is_still_detected_and_logged() -> None:
    siris = _FakeSirisForPresence(_AWAY_CLIENTS, ["brads-iphone"])  # no away_scene set
    siris.events.log_event("presence_changed", entity_name="presence", data={"home": True})

    result = PresenceSkill().run(siris)

    assert result["changed"] is True
    assert result["scene_activated"] is None
    assert not any(name == "home_assistant_call_service" for name, _ in siris.run_tool_calls)


def test_transition_without_home_assistant_configured_skips_the_scene_call() -> None:
    siris = _FakeSirisForPresence(_AWAY_CLIENTS, ["brads-iphone"], away_scene="scene.away", has_call_service=False)
    siris.events.log_event("presence_changed", entity_name="presence", data={"home": True})

    result = PresenceSkill().run(siris)

    assert result["changed"] is True
    assert result["scene_activated"] is None


# -- Registration gating (SirisAI + real Settings)


def test_presence_tool_not_registered_without_unifi_configured() -> None:
    siris = SirisAI(settings=Settings(presence_device_names="brads-iphone"))
    assert siris.tools.get("presence_check") is None


def test_presence_tool_not_registered_without_device_names() -> None:
    siris = SirisAI(settings=Settings(unifi_base_url="https://unifi.local", unifi_api_key="secret"))
    try:
        assert siris.tools.get("presence_check") is None
    finally:
        assert siris._unifi_client is not None
        siris._unifi_client.close()


def test_presence_tool_registered_when_unifi_and_device_names_configured() -> None:
    siris = SirisAI(
        settings=Settings(
            unifi_base_url="https://unifi.local", unifi_api_key="secret", presence_device_names="brads-iphone"
        )
    )
    try:
        tool = siris.tools.get("presence_check")
        assert tool is not None
        from app.plugins.base import ToolPermission

        assert tool.permission is ToolPermission.SAFE_ACTION
        assert "presence_check" in siris.capabilities()["skills"]
    finally:
        assert siris._unifi_client is not None
        siris._unifi_client.close()


# -- Endpoint-level: POST /siris/proactive/presence


def test_presence_endpoint_404s_when_not_configured(client: TestClient) -> None:
    response = client.post("/siris/proactive/presence")
    assert response.status_code == 404


def test_presence_endpoint_runs_the_check(client: TestClient) -> None:
    fake_siris = _FakeSirisForPresence(_HOME_CLIENTS, ["brads-iphone"])
    app.dependency_overrides[get_siris] = lambda: fake_siris
    try:
        response = client.post("/siris/proactive/presence")
        assert response.status_code == 200
        assert response.json()["result"]["currently_home"] is True
    finally:
        app.dependency_overrides.pop(get_siris, None)
