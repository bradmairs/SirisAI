from typing import Any

from fastapi.testclient import TestClient

from app.core.siris import get_siris
from app.main import app


class _FakeTools:
    def __init__(self, notify_present: bool) -> None:
        self._notify_present = notify_present

    def get(self, name: str) -> object | None:
        return object() if name == "notify" and self._notify_present else None


class _FakeEntities:
    """No relationships recorded by default -- nothing correlates, same
    behavior as the old flat logic for tests that aren't about
    correlation. `relationships` lets a specific test wire one up."""

    def __init__(self, relationships: dict[str, dict[str, list[Any]]] | None = None) -> None:
        self._relationships = relationships or {}

    def relationships_for(self, entity_name: str) -> dict[str, list[Any]]:
        return self._relationships.get(entity_name, {"outgoing": [], "incoming": []})


class _FakeSirisForProactive:
    def __init__(
        self,
        health_result: dict[str, Any],
        notify_present: bool,
        entities: _FakeEntities | None = None,
        event_timing_result: dict[str, Any] | None = None,
        goal_check_in_result: dict[str, Any] | None = None,
        new_device_alert_result: dict[str, Any] | None = None,
    ) -> None:
        self.tools = _FakeTools(notify_present)
        self.entities = entities or _FakeEntities()
        self._health_result = health_result
        self._event_timing_result = event_timing_result or {"healthy": True, "warnings": [], "overdue_events": []}
        self._goal_check_in_result = goal_check_in_result or {"healthy": True, "warnings": [], "stale_goals": []}
        self._new_device_alert_result = new_device_alert_result or {"checked": True, "new_devices": [], "warnings": []}
        self.notify_calls: list[dict[str, Any]] = []

    def run_tool(self, name: str, arguments: dict[str, Any], **kwargs: Any) -> Any:
        if name == "server_health_check":
            return self._health_result
        if name == "event_timing_check":
            return self._event_timing_result
        if name == "goal_check_in":
            return self._goal_check_in_result
        if name == "new_device_alert":
            return self._new_device_alert_result
        if name == "notify":
            self.notify_calls.append(arguments)
            return {"sent": True}
        raise AssertionError(f"unexpected tool call: {name}")


def test_proactive_check_notifies_when_unhealthy(client: TestClient) -> None:
    fake_siris = _FakeSirisForProactive({"healthy": False, "warnings": ["CPU usage is high: 95%"]}, notify_present=True)
    app.dependency_overrides[get_siris] = lambda: fake_siris
    try:
        response = client.post("/siris/proactive/check")
        assert response.status_code == 200
        body = response.json()
        assert body["notified"] is True
        assert body["result"]["healthy"] is False
        assert fake_siris.notify_calls == [
            {"title": "SirisAI: server health check", "message": "CPU usage is high: 95%", "priority": None}
        ]
    finally:
        app.dependency_overrides.pop(get_siris, None)


def test_proactive_check_does_not_notify_when_healthy(client: TestClient) -> None:
    fake_siris = _FakeSirisForProactive({"healthy": True, "warnings": []}, notify_present=True)
    app.dependency_overrides[get_siris] = lambda: fake_siris
    try:
        response = client.post("/siris/proactive/check")
        assert response.json()["notified"] is False
        assert fake_siris.notify_calls == []
    finally:
        app.dependency_overrides.pop(get_siris, None)


def test_proactive_check_does_not_notify_without_a_configured_channel(client: TestClient) -> None:
    fake_siris = _FakeSirisForProactive({"healthy": False, "warnings": ["disk full"]}, notify_present=False)
    app.dependency_overrides[get_siris] = lambda: fake_siris
    try:
        response = client.post("/siris/proactive/check")
        assert response.json()["notified"] is False
    finally:
        app.dependency_overrides.pop(get_siris, None)


def test_proactive_check_real_end_to_end_without_notify_configured(client: TestClient) -> None:
    # Default test settings have no ntfy topic, so this should never
    # notify regardless of the real health check's outcome.
    response = client.post("/siris/proactive/check")
    assert response.status_code == 200
    body = response.json()
    assert "healthy" in body["result"]
    assert body["notified"] is False


def test_proactive_check_correlates_related_warnings_into_one_notification(client: TestClient) -> None:
    entities = _FakeEntities({"plex": {"outgoing": [{"object_entity": "media-storage"}], "incoming": []}})
    fake_siris = _FakeSirisForProactive(
        {"healthy": False, "warnings": ["Container 'plex' is exited", "Disk usage on media-storage is high: 96%"]},
        notify_present=True,
        entities=entities,
    )
    app.dependency_overrides[get_siris] = lambda: fake_siris
    try:
        response = client.post("/siris/proactive/check")
        assert response.status_code == 200
        body = response.json()

        assert len(body["notifications"]) == 1
        assert body["notifications"][0]["category"] == "correlated"
        assert body["notifications"][0]["action"] == "urgent"

        assert len(fake_siris.notify_calls) == 1
        assert fake_siris.notify_calls[0]["title"] == "SirisAI: urgent"
        assert "Container 'plex' is exited" in fake_siris.notify_calls[0]["message"]
        assert "Disk usage on media-storage is high" in fake_siris.notify_calls[0]["message"]
        # Escalation tiers (milestone 44): an urgent item raises the
        # notification's ntfy priority, not just its title.
        assert fake_siris.notify_calls[0]["priority"] == "urgent"
    finally:
        app.dependency_overrides.pop(get_siris, None)


def test_proactive_check_exposes_notifications_even_when_nothing_sent(client: TestClient) -> None:
    fake_siris = _FakeSirisForProactive(
        {"healthy": False, "warnings": ["CPU usage is high: 95%"]}, notify_present=False
    )
    app.dependency_overrides[get_siris] = lambda: fake_siris
    try:
        response = client.post("/siris/proactive/check")
        body = response.json()
        assert body["notified"] is False
        assert len(body["notifications"]) == 1
        assert body["notifications"][0]["action"] == "notify"
    finally:
        app.dependency_overrides.pop(get_siris, None)
