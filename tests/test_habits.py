"""Habit suggestions (app/anticipation/habits.py)."""

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.anticipation import habits
from app.core.config import Settings
from app.core.scheduler import run_habit_check
from app.core.siris import SirisAI
from app.home_assistant.client import HomeAssistantClient
from app.plugins.base import ConfirmationRequired

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)  # a Thursday


def _entry(day: datetime, hh: int, mm: int, entity: str, state: str, **extra: Any) -> dict[str, Any]:
    when = day.replace(hour=hh, minute=mm, second=0, microsecond=0)
    return {"when": when.isoformat(), "entity_id": entity, "state": state, "name": entity.split(".")[1].replace("_", " ").title(), **extra}


def _logbook() -> list[dict[str, Any]]:
    entries = []
    for i in range(1, 15):
        day = NOW - timedelta(days=i)
        if day.weekday() < 5:
            # Office light on around 07:10 on 9 of 10 weekdays.
            if i != 3:
                entries.append(_entry(day, 7, 5 + (i % 3) * 5, "light.office", "on", context_user_id="u1"))
            # Random kitchen activity: no pattern.
            entries.append(_entry(day, 6 + (i * 5) % 14, 0, "light.kitchen", "on"))
        else:
            # Weekend: TV on around 19:30 both weekends.
            entries.append(_entry(day, 19, 25 + i % 2 * 10, "media_player.tv", "playing"))
        # Porch light on at 18:00 every day -- but by an automation already.
        entries.append(_entry(day, 18, 0, "light.porch", "on", context_event_type="automation_triggered", context_domain="automation"))
        # Sensors and other domains never count.
        entries.append(_entry(day, 7, 0, "sensor.temperature", "21"))
    return entries


def test_mining_finds_real_habits_and_ignores_noise_and_automated_changes() -> None:
    found = {h["id"]: h for h in habits.mine(_logbook(), NOW, "UTC", lookback_days=14, min_days=5)}
    assert set(found) == {"light.office|on|weekday", "media_player.tv|playing|weekend"}
    office = found["light.office|on|weekday"]
    assert office["time"] == "07:10" and office["days_seen"] == 9 and office["days_possible"] == 10
    assert office["description"] == "You turn on Office around 07:10 on weekdays (9 of 10)."
    assert office["automation"]["trigger"] == [{"platform": "time", "at": "07:10:00"}]
    assert office["automation"]["condition"][0]["weekday"] == ["mon", "tue", "wed", "thu", "fri"]
    assert office["automation"]["action"] == [{"service": "light.turn_on", "target": {"entity_id": "light.office"}}]
    tv = found["media_player.tv|playing|weekend"]
    assert tv["automation"]["action"][0]["service"] == "media_player.media_play"


def test_timezones_shift_the_habit_time() -> None:
    found = {h["id"]: h for h in habits.mine(_logbook(), NOW, "Australia/Melbourne", 14, 5)}
    assert found["light.office|on|weekday"]["time"] == "17:10"


def _siris(logbook: list[dict[str, Any]], created: list[dict[str, Any]]) -> SirisAI:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/api/logbook"):
            return httpx.Response(200, json=logbook)
        if request.url.path.startswith("/api/config/automation/config/"):
            created.append(json.loads(request.content))
            return httpx.Response(200, json={"result": "ok"})
        return httpx.Response(404)

    siris = SirisAI(settings=Settings(home_assistant_base_url="http://ha", home_assistant_token="t", timezone="UTC", ntfy_topic="t", public_base_url="https://siris.example"))
    client = HomeAssistantClient(base_url="http://ha", token="t", transport=httpx.MockTransport(handler))
    siris._home_assistant_client = client
    siris.tools.get("home_assistant_create_automation")._client = client  # type: ignore[union-attr]
    return siris


@pytest.fixture(autouse=True)
def _reset(client: TestClient) -> None:
    SirisAI(settings=Settings()).state.delete(habits.STATE_KEY)


def test_suggest_accept_and_dismiss_through_the_tools() -> None:
    created: list[dict[str, Any]] = []
    siris = _siris(_logbook(), created)
    from unittest.mock import patch

    with patch("app.anticipation.habits.datetime") as fake_dt:
        fake_dt.now.return_value = NOW
        fake_dt.fromisoformat = datetime.fromisoformat
        first = siris.run_tool("habit_suggestions", {})
        again = siris.run_tool("habit_suggestions", {})
    assert sorted(first["new"]) == ["light.office|on|weekday", "media_player.tv|playing|weekend"]
    assert again["new"] == []  # known suggestions aren't "new" twice

    with pytest.raises(ConfirmationRequired):
        siris.run_tool("accept_habit_suggestion", {"suggestion_id": "light.office|on|weekday"})
    accepted = siris.run_tool("accept_habit_suggestion", {"suggestion_id": "light.office|on|weekday"}, confirmed=True)
    assert created[0]["alias"] == "Office turn on at 07:10 (weekdays)"
    assert accepted["automation"]["automation_id"]
    siris.run_tool("dismiss_habit_suggestion", {"suggestion_id": "media_player.tv|playing|weekend"})
    assert habits.suggestions(siris) == []


def test_scheduled_check_pushes_with_one_tap_buttons() -> None:
    created: list[dict[str, Any]] = []
    siris = _siris(_logbook(), created)
    sent: list[dict[str, Any]] = []

    class Pushes:
        def send(self, message: str, title: str | None = None, actions: Any = None, priority: str | None = None) -> None:
            sent.append({"message": message, "actions": actions})

        def close(self) -> None:
            pass

    siris.tools.get("notify")._client = Pushes()  # type: ignore[union-attr]
    from unittest.mock import patch

    with patch("app.anticipation.habits.datetime") as fake_dt:
        fake_dt.now.return_value = NOW
        fake_dt.fromisoformat = datetime.fromisoformat
        run_habit_check(siris)
    office = next(s for s in sent if "Office" in s["message"])
    assert office["message"].endswith("Want me to automate it?")
    create = office["actions"][0]
    assert create["url"] == "https://siris.example/siris/tools/accept_habit_suggestion/run"
    assert json.loads(create["body"]) == {"arguments": {"suggestion_id": "light.office|on|weekday"}, "confirmed": True}
