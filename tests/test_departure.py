"""Departure heads-up (app/skills/builtin/departure.py)."""

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi.testclient import TestClient

from app.plugins.base import Tool, ToolPermission
from app.skills.builtin.presence import PresenceSkill
from tests.test_protocols import FakeHA, _siris


class _Pushes:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    def send(self, message: str, title: str | None = None, actions: Any = None, priority: str | None = None) -> None:
        self.sent.append({"message": message, "title": title, "actions": actions, "priority": priority})

    def close(self) -> None:
        pass


class _Static(Tool):
    permission = ToolPermission.READ
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, name: str, value: Any) -> None:
        self.name, self.description, self._value = name, name, value

    def run(self, **kwargs: Any) -> Any:
        return self._value


def _setup(client: TestClient, **settings: Any) -> tuple[Any, FakeHA, _Pushes]:
    ha = FakeHA()
    siris = _siris(ha, ntfy_topic="t", public_base_url="https://siris.example", **settings)
    pushes = _Pushes()
    siris.tools.get("notify")._client = pushes  # type: ignore[union-attr]
    return siris, ha, pushes


def test_heads_up_lists_what_is_open_next_event_and_rain(client: TestClient) -> None:
    siris, ha, pushes = _setup(client, timezone="UTC")
    now = datetime(2026, 10, 1, 8, 20, tzinfo=UTC)
    siris.tools.register(_Static("calendar_upcoming_events", [
        {"summary": "Dentist", "start": (now + timedelta(minutes=40)).isoformat()},
        {"summary": "Dinner", "start": (now + timedelta(hours=10)).isoformat()},
        {"summary": "Holiday", "start": "2026-10-01"},
    ]))
    siris.tools.register(_Static("weather_forecast", [{"conditions": "Light rain", "rain_chance_percent": 70}]))

    result = siris.tools.get("departure_check").run(notify=True, now=now)  # type: ignore[union-attr]

    assert result["left_open"][0] == "cover.garage_door is open"  # the blind (device_class blind) isn't listed
    assert any("unlocked" in item for item in result["left_open"])
    assert any("light" in item for item in result["left_open"])
    assert result["next_event"] == "'Dentist' at 09:00 starts in 40 min"
    assert result["rain"].startswith("70% chance of rain today")
    push = pushes.sent[0]
    assert push["priority"] == "high" and push["title"] == "SirisAI: heading out"
    assert push["actions"][0]["label"] == "Run Away protocol"
    assert json.loads(push["actions"][0]["body"]) == {"arguments": {"name": "away"}, "confirmed": True}


def test_all_good_when_nothing_is_open(client: TestClient) -> None:
    siris, ha, pushes = _setup(client)
    for entity in list(ha.states):
        ha.states[entity]["state"] = {"lock": "locked", "cover": "closed"}.get(entity.split(".")[0], "off")
    result = siris.tools.get("departure_check").run(notify=True)  # type: ignore[union-attr]
    assert result["summary"].startswith("All good")
    assert pushes.sent[0]["actions"] is None and pushes.sent[0]["priority"] == "default"


def test_presence_going_empty_triggers_the_heads_up(client: TestClient) -> None:
    siris, ha, pushes = _setup(client, presence_device_names="brads-iphone")
    clients = [{"name": "brads-iphone"}]
    siris.tools.register(_Static("unifi_list_clients", clients))
    skill = PresenceSkill()
    skill.run(siris)  # baseline (or already home from earlier tests)
    siris.events.log_event("presence_changed", entity_name="presence", data={"home": True})
    clients.clear()
    result = skill.run(siris)
    assert result["changed"] is True and result["currently_home"] is False
    assert result["departure"]["notified"] is True
    assert pushes.sent and pushes.sent[-1]["title"] == "SirisAI: heading out"
