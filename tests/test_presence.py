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


class _FakeState(dict):
    def set(self, key: str, value: Any) -> None:
        self[key] = value


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
        self.state = _FakeState()
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


def test_away_only_after_the_phones_have_been_gone_a_while() -> None:
    """5.63: a phone asleep off the Wi-Fi for a check or two isn't anyone leaving."""
    from datetime import UTC, datetime, timedelta

    siris = _FakeSirisForPresence(_AWAY_CLIENTS, ["brads-iphone"], away_scene="scene.away")
    siris.settings.presence_away_after_minutes = 10
    siris.events.log_event("presence_changed", entity_name="presence", data={"home": True})
    t0 = datetime(2026, 11, 11, 8, 0, tzinfo=UTC)
    first = PresenceSkill().run(siris, now=t0)
    assert first["currently_home"] is True and first["away_in_minutes"] == 10 and first["changed"] is False
    assert PresenceSkill().run(siris, now=t0 + timedelta(minutes=6))["currently_home"] is True
    # Back on the Wi-Fi: the clock starts again.
    siris._clients = _HOME_CLIENTS
    PresenceSkill().run(siris, now=t0 + timedelta(minutes=7))
    siris._clients = _AWAY_CLIENTS
    assert PresenceSkill().run(siris, now=t0 + timedelta(minutes=12))["currently_home"] is True
    gone = PresenceSkill().run(siris, now=t0 + timedelta(minutes=22))
    assert gone["currently_home"] is False and gone["changed"] is True and gone["scene_activated"] == "scene.away"


def test_first_check_establishes_baseline_without_activating_a_scene() -> None:
    siris = _FakeSirisForPresence(_HOME_CLIENTS, ["brads-iphone"], home_scene="scene.welcome_home")

    result = PresenceSkill().run(siris)

    assert result == {"currently_home": True, "changed": False, "scene_activated": None}
    assert siris.events.logged[0]["data"] == {"home": True, "baseline": True}   # not an arrival (hub presence: since)
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



def test_away_only_once_every_counted_phone_has_gone_and_names_match_loosely() -> None:
    """4.4: Dom's iPhone counts as home too (whichever way UniFi spells it), so leaving needs both phones gone."""
    clients = [{"name": "brads-iphone"}, {"name": None, "hostname": "Doms-iPhone"}]
    siris = _FakeSirisForPresence(clients=clients, device_names=["brads-iphone"])
    skill = PresenceSkill()
    skill.run(siris)  # baseline: home
    clients.pop(0)  # Brad leaves; Dom's still home
    result = skill.run(siris)
    assert result["currently_home"] is True and result["changed"] is False
    from app.skills.builtin.presence import counted, set_added

    assert {d["name"]: d["home"] for d in counted(siris)} == {"brads-iphone": False, "Dom's iPhone": True}
    clients.clear()  # Dom leaves too
    result = skill.run(siris)
    assert result["currently_home"] is False and result["changed"] is True

    set_added(siris, ["Dom's iPhone", "Guest iPad", "dom's iphone"])
    assert [d["name"] for d in counted(siris)] == ["brads-iphone", "Dom's iPhone", "Guest iPad"]


def test_away_mode_over_the_hub(client: TestClient) -> None:
    """5.63: SirisOS sets what happens when everyone's out (Apple Home may already do it)."""
    from app.core import settings_overrides

    siris = SirisAI(settings=Settings(away_mode="auto"))
    app.dependency_overrides[get_siris] = lambda: siris
    try:
        assert client.get("/siris/hub/v1/away").json() == {"mode": "auto", "protocol": "away", "away_after_minutes": 10.0}
        assert client.put("/siris/hub/v1/away", json={"mode": "off"}).json()["mode"] == "off"
        assert siris.settings.away_mode == "off" and settings_overrides.load().get("away_mode") == "off"
        assert client.put("/siris/hub/v1/away", json={"mode": "sometimes"}).status_code == 422
    finally:
        app.dependency_overrides.pop(get_siris, None)
        settings_overrides.update(siris, {"away_mode": "ask"})
