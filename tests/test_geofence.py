import uuid
from typing import Any

from fastapi.testclient import TestClient

from app.core.siris import SirisAI, get_siris
from app.main import app
from app.plugins.base import ToolPermission
from app.skills.builtin.geofence_reminder import GeofenceReminderCheckSkill


def _fresh_siris() -> SirisAI:
    return SirisAI(settings=get_siris().settings)


# -- GeofenceReminderStore


def test_create_returns_expected_shape() -> None:
    siris = _fresh_siris()
    entity_id = f"person.test-{uuid.uuid4()}"

    reminder = siris.geofence.create(entity_id, "home", "pick up the mail")

    assert reminder["entity_id"] == entity_id
    assert reminder["zone"] == "home"
    assert reminder["message"] == "pick up the mail"
    assert reminder["repeat"] is False
    assert reminder["enabled"] is True
    assert reminder["last_known_zone"] is None
    assert isinstance(reminder["id"], int)


def test_list_all_enabled_only_excludes_disabled() -> None:
    siris = _fresh_siris()
    marker = str(uuid.uuid4())
    enabled = siris.geofence.create(f"person.{marker}-a", "home", "msg")
    disabled = siris.geofence.create(f"person.{marker}-b", "home", "msg")
    siris.geofence.set_enabled(disabled["id"], False)

    ids = {r["id"] for r in siris.geofence.list_all(enabled_only=True)}
    assert enabled["id"] in ids
    assert disabled["id"] not in ids


def test_set_enabled_returns_none_for_unknown_id() -> None:
    siris = _fresh_siris()
    assert siris.geofence.set_enabled(999999999, False) is None


def test_record_zone_updates_baseline() -> None:
    siris = _fresh_siris()
    reminder = siris.geofence.create(f"person.{uuid.uuid4()}", "home", "msg")

    siris.geofence.record_zone(reminder["id"], "work")

    updated = next(r for r in siris.geofence.list_all() if r["id"] == reminder["id"])
    assert updated["last_known_zone"] == "work"
    assert updated["enabled"] is True


def test_record_zone_can_disable() -> None:
    siris = _fresh_siris()
    reminder = siris.geofence.create(f"person.{uuid.uuid4()}", "home", "msg")

    siris.geofence.record_zone(reminder["id"], "home", disable=True)

    updated = next(r for r in siris.geofence.list_all() if r["id"] == reminder["id"])
    assert updated["enabled"] is False


# -- Tools: permission tiers and registration


def test_geofence_tools_registered_by_default_with_expected_permissions() -> None:
    siris = _fresh_siris()
    create_tool = siris.tools.get("create_geofence_reminder")
    list_tool = siris.tools.get("list_geofence_reminders")
    set_enabled_tool = siris.tools.get("set_geofence_reminder_enabled")

    assert create_tool is not None and create_tool.permission is ToolPermission.SAFE_ACTION
    assert list_tool is not None and list_tool.permission is ToolPermission.READ
    assert (
        set_enabled_tool is not None
        and set_enabled_tool.permission is ToolPermission.SAFE_ACTION
    )


def test_create_and_list_tools_round_trip() -> None:
    siris = _fresh_siris()
    entity_id = f"person.{uuid.uuid4()}"

    created = siris.run_tool(
        "create_geofence_reminder",
        {"entity_id": entity_id, "zone": "home", "message": "welcome back"},
    )
    listed = siris.run_tool("list_geofence_reminders", {"enabled_only": True})

    assert any(r["id"] == created["id"] for r in listed)


# -- GeofenceReminderCheckSkill


class _FakeGeofenceStore:
    def __init__(self, reminders: list[dict[str, Any]]) -> None:
        self._reminders = {r["id"]: r for r in reminders}
        self.record_zone_calls: list[tuple[int, str, bool]] = []

    def list_all(self, enabled_only: bool = False) -> list[dict[str, Any]]:
        values = list(self._reminders.values())
        return [r for r in values if r["enabled"]] if enabled_only else values

    def record_zone(self, reminder_id: int, zone: str, *, disable: bool = False) -> None:
        self.record_zone_calls.append((reminder_id, zone, disable))
        self._reminders[reminder_id]["last_known_zone"] = zone
        if disable:
            self._reminders[reminder_id]["enabled"] = False


class _FakeTools:
    def __init__(self, has_home_assistant: bool = True, has_notify: bool = True) -> None:
        self._has_home_assistant = has_home_assistant
        self._has_notify = has_notify

    def get(self, name: str) -> object | None:
        if name == "home_assistant_get_state":
            return object() if self._has_home_assistant else None
        if name == "notify":
            return object() if self._has_notify else None
        return None


def _reminder(
    id: int, entity_id: str, zone: str, message: str, last_known_zone: str | None, repeat: bool = False
) -> dict[str, Any]:
    return {
        "id": id,
        "entity_id": entity_id,
        "zone": zone,
        "message": message,
        "repeat": repeat,
        "enabled": True,
        "last_known_zone": last_known_zone,
    }


class _FakeSirisForGeofence:
    def __init__(
        self,
        reminders: list[dict[str, Any]],
        states_by_entity: dict[str, str],
        has_home_assistant: bool = True,
        has_notify: bool = True,
        raise_for_entity: str | None = None,
    ) -> None:
        self.tools = _FakeTools(has_home_assistant, has_notify)
        self.geofence = _FakeGeofenceStore(reminders)
        self._states_by_entity = states_by_entity
        self._raise_for_entity = raise_for_entity
        self.notify_calls: list[dict[str, Any]] = []

    def run_tool(self, name: str, arguments: dict[str, Any], **kwargs: Any) -> Any:
        if name == "home_assistant_get_state":
            entity_id = arguments["entity_id"]
            if entity_id == self._raise_for_entity:
                raise RuntimeError("entity not found")
            return {"entity_id": entity_id, "state": self._states_by_entity[entity_id]}
        if name == "notify":
            self.notify_calls.append(arguments)
            return {"sent": True}
        raise AssertionError(f"unexpected tool call: {name}")


def test_skips_the_check_when_home_assistant_is_not_configured() -> None:
    siris = _FakeSirisForGeofence([], {}, has_home_assistant=False)
    result = GeofenceReminderCheckSkill().run(siris)
    assert result == {"checked": False, "fired": []}


def test_first_ever_check_establishes_baseline_without_firing() -> None:
    reminder = _reminder(1, "person.brad", "home", "msg", last_known_zone=None)
    siris = _FakeSirisForGeofence([reminder], {"person.brad": "home"})

    result = GeofenceReminderCheckSkill().run(siris)

    assert result == {"checked": True, "fired": []}
    assert siris.geofence.record_zone_calls == [(1, "home", False)]
    assert siris.notify_calls == []


def test_no_zone_change_does_not_fire() -> None:
    reminder = _reminder(1, "person.brad", "home", "msg", last_known_zone="home")
    siris = _FakeSirisForGeofence([reminder], {"person.brad": "home"})

    result = GeofenceReminderCheckSkill().run(siris)

    assert result == {"checked": True, "fired": []}
    assert siris.geofence.record_zone_calls == []
    assert siris.notify_calls == []


def test_transition_into_watched_zone_fires_and_notifies() -> None:
    reminder = _reminder(1, "person.brad", "home", "welcome home", last_known_zone="work")
    siris = _FakeSirisForGeofence([reminder], {"person.brad": "home"})

    result = GeofenceReminderCheckSkill().run(siris)

    assert result == {"checked": True, "fired": [{"id": 1, "message": "welcome home"}]}
    assert siris.notify_calls == [{"title": "SirisAI: reminder", "message": "welcome home"}]


def test_non_repeating_reminder_disables_itself_after_firing() -> None:
    reminder = _reminder(1, "person.brad", "home", "msg", last_known_zone="work", repeat=False)
    siris = _FakeSirisForGeofence([reminder], {"person.brad": "home"})

    GeofenceReminderCheckSkill().run(siris)

    assert siris.geofence.record_zone_calls == [(1, "home", True)]


def test_repeating_reminder_stays_enabled_after_firing() -> None:
    reminder = _reminder(1, "person.brad", "home", "msg", last_known_zone="work", repeat=True)
    siris = _FakeSirisForGeofence([reminder], {"person.brad": "home"})

    GeofenceReminderCheckSkill().run(siris)

    assert siris.geofence.record_zone_calls == [(1, "home", False)]


def test_transition_to_a_different_zone_updates_baseline_without_firing() -> None:
    reminder = _reminder(1, "person.brad", "home", "msg", last_known_zone="work")
    siris = _FakeSirisForGeofence([reminder], {"person.brad": "school"})

    result = GeofenceReminderCheckSkill().run(siris)

    assert result == {"checked": True, "fired": []}
    assert siris.geofence.record_zone_calls == [(1, "school", False)]


def test_already_in_watched_zone_does_not_refire() -> None:
    """previous == zone == current -- already there, not a fresh
    transition, even though current == the watched zone."""
    reminder = _reminder(1, "person.brad", "home", "msg", last_known_zone="home")
    siris = _FakeSirisForGeofence([reminder], {"person.brad": "home"})

    result = GeofenceReminderCheckSkill().run(siris)

    assert result == {"checked": True, "fired": []}
    assert siris.geofence.record_zone_calls == []


def test_fires_even_without_notify_configured() -> None:
    reminder = _reminder(1, "person.brad", "home", "msg", last_known_zone="work")
    siris = _FakeSirisForGeofence([reminder], {"person.brad": "home"}, has_notify=False)

    result = GeofenceReminderCheckSkill().run(siris)

    assert result == {"checked": True, "fired": [{"id": 1, "message": "msg"}]}
    assert siris.notify_calls == []


def test_one_reminders_entity_erroring_does_not_block_the_others() -> None:
    broken = _reminder(1, "person.gone", "home", "broken", last_known_zone="work")
    healthy = _reminder(2, "person.brad", "home", "healthy", last_known_zone="work")
    siris = _FakeSirisForGeofence(
        [broken, healthy],
        {"person.brad": "home"},
        raise_for_entity="person.gone",
    )

    result = GeofenceReminderCheckSkill().run(siris)

    assert result == {"checked": True, "fired": [{"id": 2, "message": "healthy"}]}


def test_disabled_reminders_are_not_checked() -> None:
    reminder = _reminder(1, "person.brad", "home", "msg", last_known_zone="work")
    reminder["enabled"] = False
    siris = _FakeSirisForGeofence([reminder], {"person.brad": "home"})

    result = GeofenceReminderCheckSkill().run(siris)

    assert result == {"checked": True, "fired": []}


# -- Endpoint: POST /siris/proactive/geofence


def test_geofence_endpoint_404s_when_not_configured(client: TestClient) -> None:
    response = client.post("/siris/proactive/geofence")
    assert response.status_code == 404


class _FakeToolsForEndpoint:
    def get(self, name: str) -> object | None:
        return object() if name == "geofence_reminder_check" else None


class _FakeSirisForGeofenceEndpoint:
    def __init__(self) -> None:
        self.tools = _FakeToolsForEndpoint()
        self.calls: list[dict[str, Any]] = []

    def run_tool(self, name: str, arguments: dict[str, Any], **kwargs: Any) -> Any:
        self.calls.append({"name": name, "arguments": arguments, **kwargs})
        return {"checked": True, "fired": []}


def test_geofence_endpoint_runs_the_check_when_configured(client: TestClient) -> None:
    fake_siris = _FakeSirisForGeofenceEndpoint()
    app.dependency_overrides[get_siris] = lambda: fake_siris
    try:
        response = client.post("/siris/proactive/geofence")
        assert response.status_code == 200
        assert response.json() == {"result": {"checked": True, "fired": []}}
        assert fake_siris.calls[0]["name"] == "geofence_reminder_check"
    finally:
        app.dependency_overrides.pop(get_siris, None)
