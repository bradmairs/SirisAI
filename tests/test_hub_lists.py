"""6.88: Home Assistant's to-do lists through the hub, and UV in the forecast."""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.siris import SirisAI, get_siris
from app.main import app
from app.weather.client import _uv_windows

H: dict[str, str] = {}


class FakeHA:
    def __init__(self) -> None:
        self.items = {"todo.shopping_list": [{"summary": "Milk", "status": "needs_action"}, {"summary": "Eggs", "status": "completed"}]}
        self.calls: list[tuple] = []

    def list_states(self, domain: str | None = None) -> list[dict]:
        return [{"entity_id": "todo.shopping_list", "attributes": {"friendly_name": "Shopping List"}}, {"entity_id": "light.lounge"}]

    def get_todo_items(self, entity_id: str, status: str | None = "needs_action") -> list[dict]:
        return list(self.items[entity_id])

    def add_todo_item(self, entity_id: str, item: str) -> None:
        self.items[entity_id].append({"summary": item, "status": "needs_action"})

    def call_service(self, domain: str, service: str, entity_id: str | None = None, **data: Any) -> None:
        self.calls.append((domain, service, entity_id, data))
        for i in self.items[entity_id]:
            if i["summary"] == data["item"]:
                i["status"] = data["status"]


def test_lists_add_and_tick(client: TestClient) -> None:
    siris = SirisAI(settings=Settings(timezone="UTC"))
    siris._home_assistant_client = FakeHA()
    app.dependency_overrides[get_siris] = lambda: siris
    try:
        got = client.get("/siris/hub/v1/lists", headers=H).json()
        assert got["available"] and got["lists"][0]["name"] == "Shopping List"
        assert [(i["summary"], i["status"]) for i in got["lists"][0]["items"]] == [("Milk", "needs_action"), ("Eggs", "completed")]
        got = client.post("/siris/hub/v1/lists/todo.shopping_list/items", headers=H, json={"item": "Bread"}).json()
        assert "Bread" in [i["summary"] for i in got["lists"][0]["items"]]
        got = client.post("/siris/hub/v1/lists/todo.shopping_list/items/done", headers=H, json={"item": "Milk", "done": True}).json()
        assert ("Milk", "completed") in [(i["summary"], i["status"]) for i in got["lists"][0]["items"]]
        assert client.post("/siris/hub/v1/lists/light.lounge/items", headers=H, json={"item": "x"}).status_code == 404
    finally:
        app.dependency_overrides.pop(get_siris, None)


def test_uv_windows_from_hourly() -> None:
    hourly = {"time": [f"2026-11-19T{h:02d}:00" for h in range(24)], "uv_index": [0] * 8 + [2, 3.5, 6, 9, 11, 10, 8, 5, 2.9] + [0] * 7}
    assert _uv_windows(hourly) == {"2026-11-19": {"sun_protection_from": "09:00", "sun_protection_to": "16:00"}}
