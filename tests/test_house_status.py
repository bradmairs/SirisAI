from typing import Any

from app.skills.builtin.house_status import HouseStatusSkill


class _FakeTools:
    def __init__(self, has_home_assistant: bool = True) -> None:
        self._has_home_assistant = has_home_assistant

    def get(self, name: str) -> object | None:
        if name == "home_assistant_list_entities":
            return object() if self._has_home_assistant else None
        return None


class _FakeSirisForHouseStatus:
    def __init__(
        self,
        binary_sensors: list[dict[str, Any]] | None = None,
        locks: list[dict[str, Any]] | None = None,
        alarm_panels: list[dict[str, Any]] | None = None,
        has_home_assistant: bool = True,
    ) -> None:
        self.tools = _FakeTools(has_home_assistant)
        self._binary_sensors = binary_sensors or []
        self._locks = locks or []
        self._alarm_panels = alarm_panels or []
        self.run_tool_calls: list[tuple[str, dict[str, Any]]] = []

    def run_tool(self, name: str, arguments: dict[str, Any], **kwargs: Any) -> Any:
        self.run_tool_calls.append((name, arguments))
        assert name == "home_assistant_list_entities"
        domain = arguments["domain"]
        if domain == "binary_sensor":
            return self._binary_sensors
        if domain == "lock":
            return self._locks
        if domain == "alarm_control_panel":
            return self._alarm_panels
        raise AssertionError(f"unexpected domain: {domain}")


def test_not_configured_without_home_assistant() -> None:
    siris = _FakeSirisForHouseStatus(has_home_assistant=False)

    result = HouseStatusSkill().run(siris)

    assert result == {"checked": False, "reason": "Home Assistant is not configured."}
    assert siris.run_tool_calls == []


def test_all_secure_when_nothing_is_open_or_unlocked() -> None:
    siris = _FakeSirisForHouseStatus(
        binary_sensors=[{"entity_id": "binary_sensor.front_door", "state": "off"}],
        locks=[{"entity_id": "lock.front_door", "state": "locked"}],
        alarm_panels=[{"entity_id": "alarm_control_panel.home", "state": "armed_away"}],
    )

    result = HouseStatusSkill().run(siris)

    assert result == {
        "checked": True,
        "open_openings": [],
        "unlocked_locks": [],
        "alarm_panels": [
            {"entity_id": "alarm_control_panel.home", "state": "armed_away"}
        ],
        "warnings": [],
        "all_secure": True,
    }


def test_open_door_is_flagged() -> None:
    siris = _FakeSirisForHouseStatus(
        binary_sensors=[
            {"entity_id": "binary_sensor.front_door", "state": "on"},
            {"entity_id": "binary_sensor.motion_hallway", "state": "on"},
        ],
    )

    result = HouseStatusSkill().run(siris)

    assert result["open_openings"] == ["binary_sensor.front_door"]
    assert result["all_secure"] is False
    assert result["warnings"] == ["Open: binary_sensor.front_door"]


def test_open_window_and_garage_are_flagged_but_closed_ones_are_not() -> None:
    siris = _FakeSirisForHouseStatus(
        binary_sensors=[
            {"entity_id": "binary_sensor.kitchen_window", "state": "on"},
            {"entity_id": "binary_sensor.garage_door", "state": "on"},
            {"entity_id": "binary_sensor.bedroom_window", "state": "off"},
        ],
    )

    result = HouseStatusSkill().run(siris)

    assert result["open_openings"] == [
        "binary_sensor.kitchen_window",
        "binary_sensor.garage_door",
    ]


def test_unlocked_lock_is_flagged() -> None:
    siris = _FakeSirisForHouseStatus(
        locks=[
            {"entity_id": "lock.back_door", "state": "unlocked"},
            {"entity_id": "lock.front_door", "state": "locked"},
        ],
    )

    result = HouseStatusSkill().run(siris)

    assert result["unlocked_locks"] == ["lock.back_door"]
    assert result["all_secure"] is False
    assert result["warnings"] == ["Unlocked: lock.back_door"]


def test_alarm_panels_are_passed_through_without_generating_warnings() -> None:
    siris = _FakeSirisForHouseStatus(
        alarm_panels=[{"entity_id": "alarm_control_panel.home", "state": "disarmed"}],
    )

    result = HouseStatusSkill().run(siris)

    assert result["alarm_panels"] == [
        {"entity_id": "alarm_control_panel.home", "state": "disarmed"}
    ]
    assert result["warnings"] == []
    assert result["all_secure"] is True


def test_multiple_warnings_combine() -> None:
    siris = _FakeSirisForHouseStatus(
        binary_sensors=[{"entity_id": "binary_sensor.front_door", "state": "on"}],
        locks=[{"entity_id": "lock.back_door", "state": "unlocked"}],
    )

    result = HouseStatusSkill().run(siris)

    assert result["all_secure"] is False
    assert result["warnings"] == [
        "Open: binary_sensor.front_door",
        "Unlocked: lock.back_door",
    ]
