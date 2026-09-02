from typing import Any

from fastapi.testclient import TestClient

from app.core.siris import get_siris
from app.main import app


class _FakeTools:
    def __init__(self, notify_present: bool) -> None:
        self._notify_present = notify_present

    def get(self, name: str) -> object | None:
        return object() if name == "notify" and self._notify_present else None


class _FakeSirisForProactive:
    def __init__(self, health_result: dict[str, Any], notify_present: bool) -> None:
        self.tools = _FakeTools(notify_present)
        self._health_result = health_result
        self.notify_calls: list[dict[str, Any]] = []

    def run_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        if name == "server_health_check":
            return self._health_result
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
            {"title": "SirisAI: server health check", "message": "CPU usage is high: 95%"}
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
