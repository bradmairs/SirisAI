import uuid

from fastapi.testclient import TestClient

from app.core.config import Settings, get_settings
from app.main import app


def test_receive_event_creates_and_returns_event(client: TestClient) -> None:
    entity_name = f"Plex-{uuid.uuid4()}"
    response = client.post(
        "/siris/events",
        json={
            "source": "docker",
            "description": "container unhealthy",
            "entity_name": entity_name,
            "data": {"exit_code": 137},
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "docker"
    assert body["description"] == "container unhealthy"
    assert body["entity_name"] == entity_name
    assert body["data"] == {"exit_code": 137}
    assert isinstance(body["id"], int)


def test_list_events_filters_by_entity_and_orders_most_recent_first(client: TestClient) -> None:
    entity_name = f"Plex-{uuid.uuid4()}"
    client.post("/siris/events", json={"source": "docker", "description": "first", "entity_name": entity_name})
    client.post("/siris/events", json={"source": "docker", "description": "second", "entity_name": entity_name})
    client.post("/siris/events", json={"source": "docker", "description": "unrelated"})

    response = client.get("/siris/events", params={"entity_name": entity_name})
    assert response.status_code == 200
    descriptions = [e["description"] for e in response.json()]
    assert descriptions == ["second", "first"]


def test_webhook_event_is_visible_via_recall_events_tool() -> None:
    from app.core.siris import SirisAI

    siris = SirisAI(settings=get_settings())
    entity_name = f"NAS-{uuid.uuid4()}"
    siris.events.log_event("disk nearly full", entity_name=entity_name, source="cron", data={"percent": 92})

    events = siris.tools.get("recall_events").run(entity_name=entity_name)
    assert events[0]["description"] == "disk nearly full"
    assert events[0]["source"] == "cron"
    assert events[0]["data"] == {"percent": 92}


def test_events_route_requires_api_key_when_configured(client: TestClient) -> None:
    app.dependency_overrides[get_settings] = lambda: Settings(api_key="secret-key")
    try:
        response = client.post("/siris/events", json={"source": "docker", "description": "x"})
        assert response.status_code == 401
    finally:
        app.dependency_overrides.pop(get_settings, None)
