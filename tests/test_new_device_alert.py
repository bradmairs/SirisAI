from typing import Any

from fastapi.testclient import TestClient

from app.core.siris import SirisAI, get_siris
from app.main import app
from app.skills.builtin.new_device_alert import NewDeviceAlertSkill


class _FakeTools:
    def __init__(self, has_home_assistant: bool = True) -> None:
        self._has_home_assistant = has_home_assistant

    def get(self, name: str) -> object | None:
        if name == "notify":
            return None  # /check's endpoint-level test here never configures a notify channel
        if name == "new_device_alert":
            return object()  # the route checks this before calling run_tool()
        return (
            object()
            if name == "home_assistant_list_entities" and self._has_home_assistant
            else None
        )


class _FakeKnownDeviceStore:
    def __init__(self, known: set[str] | None = None) -> None:
        self._known = set(known or set())
        self.mark_known_calls: list[set[str]] = []

    def list_known(self) -> set[str]:
        return set(self._known)

    def mark_known(self, entity_ids: Any) -> None:
        ids = set(entity_ids)
        self.mark_known_calls.append(ids)
        self._known |= ids


class _FakePendingNotifications:
    """Interruption controller (milestone 72) -- POST /siris/proactive/check
    queues "notify"-tier items here instead of pushing them; this test file
    isn't about that queuing itself, just needs the attribute present."""

    def queue(self, message: str, severity: str, category: str) -> None:
        pass


class _FakeSirisForNewDeviceAlert:
    def __init__(
        self,
        entities: list[dict[str, Any]],
        known: set[str] | None = None,
        has_home_assistant: bool = True,
    ) -> None:
        self.tools = _FakeTools(has_home_assistant)
        self.known_devices = _FakeKnownDeviceStore(known)
        self.entities = object()  # never touched: no docker/disk warnings here for NotificationManager to correlate
        self.pending_notifications = _FakePendingNotifications()
        self._device_tracker_entities = entities
        self.run_tool_calls: list[tuple[str, dict[str, Any]]] = []

    def run_tool(self, name: str, arguments: dict[str, Any], **kwargs: Any) -> Any:
        self.run_tool_calls.append((name, arguments))
        if name == "home_assistant_list_entities":
            assert arguments == {"domain": "device_tracker"}
            return self._device_tracker_entities
        if name == "new_device_alert":
            # POST /siris/proactive/check calls run_tool("new_device_alert", ...)
            # the same way it would for a real registered skill -- route it
            # through the actual skill logic (already covered by its own
            # direct tests above) rather than re-faking its composition here.
            return NewDeviceAlertSkill().run(self)
        if name in ("server_health_check", "event_timing_check", "goal_check_in"):
            # Not this skill's concern -- POST /siris/proactive/check
            # composes them all, so the endpoint-level test needs them
            # stubbed out, not re-tested here (each has its own test file).
            return {"warnings": []}
        raise AssertionError(f"unexpected tool call: {name}")


_KNOWN_ENTITY = {"entity_id": "device_tracker.brads_phone", "state": "home"}
_NEW_ENTITY = {"entity_id": "device_tracker.unknown_device", "state": "home"}


# -- NewDeviceAlertSkill


def test_first_check_establishes_baseline_without_flagging_anything() -> None:
    siris = _FakeSirisForNewDeviceAlert([_KNOWN_ENTITY, _NEW_ENTITY])

    result = NewDeviceAlertSkill().run(siris)

    assert result == {"checked": True, "new_devices": [], "warnings": []}
    assert siris.known_devices.mark_known_calls == [
        {"device_tracker.brads_phone", "device_tracker.unknown_device"}
    ]


def test_a_new_device_is_flagged_once_a_baseline_exists() -> None:
    siris = _FakeSirisForNewDeviceAlert(
        [_KNOWN_ENTITY, _NEW_ENTITY], known={"device_tracker.brads_phone"}
    )

    result = NewDeviceAlertSkill().run(siris)

    assert result == {
        "checked": True,
        "new_devices": ["device_tracker.unknown_device"],
        "warnings": ["Unrecognized device connected: device_tracker.unknown_device"],
    }
    assert siris.known_devices.mark_known_calls == [{"device_tracker.unknown_device"}]


def test_no_new_devices_reports_nothing_and_does_not_touch_the_store() -> None:
    siris = _FakeSirisForNewDeviceAlert(
        [_KNOWN_ENTITY], known={"device_tracker.brads_phone"}
    )

    result = NewDeviceAlertSkill().run(siris)

    assert result == {"checked": True, "new_devices": [], "warnings": []}
    assert siris.known_devices.mark_known_calls == []


def test_a_device_disappearing_is_not_flagged_as_anything() -> None:
    """Only new arrivals are reported -- a previously-known device not
    currently in the list (e.g. it's out of range right now) isn't an
    "unrecognized device" concern, so it's silently left in the known set
    rather than removed or warned about."""
    siris = _FakeSirisForNewDeviceAlert([], known={"device_tracker.brads_phone"})

    result = NewDeviceAlertSkill().run(siris)

    assert result == {"checked": True, "new_devices": [], "warnings": []}


def test_skips_the_check_when_home_assistant_is_not_configured() -> None:
    siris = _FakeSirisForNewDeviceAlert([], has_home_assistant=False)

    result = NewDeviceAlertSkill().run(siris)

    assert result == {"checked": False, "new_devices": [], "warnings": []}
    assert siris.run_tool_calls == []


# -- Registration (always registered, same as server_health_check/daily_briefing)


def test_new_device_alert_tool_is_registered_as_a_skill() -> None:
    from app.core.config import Settings

    siris = SirisAI(settings=Settings())
    assert siris.tools.get("new_device_alert") is not None
    assert "new_device_alert" in siris.capabilities()["skills"]


# -- Endpoint-level: composed into POST /siris/proactive/check


def test_check_endpoint_includes_new_device_alert_results(client: TestClient) -> None:
    fake_siris = _FakeSirisForNewDeviceAlert(
        [_KNOWN_ENTITY, _NEW_ENTITY], known={"device_tracker.brads_phone"}
    )
    app.dependency_overrides[get_siris] = lambda: fake_siris
    try:
        response = client.post("/siris/proactive/check")
        assert response.status_code == 200
        body = response.json()
        assert body["new_device_alert"]["new_devices"] == [
            "device_tracker.unknown_device"
        ]
        assert any(n["category"] == "network" for n in body["notifications"])
    finally:
        app.dependency_overrides.pop(get_siris, None)
