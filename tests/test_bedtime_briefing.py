from datetime import UTC, datetime, timedelta
from typing import Any

from app.core.config import Settings
from app.core.siris import SirisAI
from app.skills.builtin.bedtime import BedtimeCheckSkill
from app.skills.builtin.daily_briefing import DailyBriefingSkill


class _Tools:
    def __init__(self, names: set[str]) -> None:
        self._names = names

    def get(self, name: str) -> object | None:
        return object() if name in self._names else None


class _FakeSiris:
    def __init__(self, outputs: dict[str, Any], real: SirisAI | None = None) -> None:
        self.tools = _Tools({n.split("|")[0] for n in outputs})
        self._outputs = {n.split("|")[0]: v for n, v in outputs.items()}
        self.settings = Settings(timezone="Australia/Melbourne")
        self.reminders = (real or SirisAI(settings=self.settings)).reminders
        self.calls: list[tuple[str, dict]] = []

    def run_tool(self, name: str, arguments: dict[str, Any], **kwargs: Any) -> Any:
        self.calls.append((name, arguments))
        value = self._outputs[name]
        if callable(value):
            return value(arguments)
        if isinstance(value, Exception):
            raise value
        return value


def _entities(arguments: dict[str, Any]) -> list[dict[str, Any]]:
    return {
        "light": [
            {"entity_id": "light.lounge_lamps", "name": "Lounge Lamps", "state": "on"},
            {"entity_id": "light.office", "name": "Office", "state": "off"},
        ],
        "media_player": [{"entity_id": "media_player.tv", "name": "Office TV", "state": "playing"}],
        "lock": [{"entity_id": "lock.front", "name": "Front Door", "state": "unlocked"}],
        "cover": [{"entity_id": "cover.garage", "name": "Garage Door", "state": "open"}],
    }.get(arguments["domain"], [])


def test_without_home_assistant_it_still_reports_tomorrow_and_says_nothing_was_checked() -> None:
    result = BedtimeCheckSkill().run(_FakeSiris({}))  # type: ignore[arg-type]
    assert result["checked"] is False and result["all_clear"] is False


def test_bedtime_lists_what_is_still_on_open_or_unlocked_with_suggestions() -> None:
    siris = _FakeSiris({"home_assistant_list_entities": _entities})
    result = BedtimeCheckSkill().run(siris)  # type: ignore[arg-type]
    assert result["lights_on"] == ["Lounge Lamps"] and result["media_playing"] == ["Office TV"]
    assert result["unlocked"] == ["Front Door"] and result["covers_open"] == ["Garage Door"]
    assert result["all_clear"] is False
    text = " ".join(result["suggestions"])
    assert "Turn off 1 light" in text and "Lock: Front Door" in text and "Garage Door" in text
    assert all(name == "home_assistant_list_entities" for name, _ in siris.calls)  # read-only


def test_a_quiet_house_is_all_clear() -> None:
    siris = _FakeSiris({"home_assistant_list_entities": lambda a: []})
    assert BedtimeCheckSkill().run(siris)["all_clear"] is True  # type: ignore[arg-type]


def test_bedtime_includes_tomorrows_first_event_weather_and_early_reminders() -> None:
    tomorrow = (datetime.now(UTC) + timedelta(days=1)).astimezone(SirisAI(settings=Settings(timezone="Australia/Melbourne")).__class__ and __import__("zoneinfo").ZoneInfo("Australia/Melbourne")).date().isoformat()
    real = SirisAI(settings=Settings(timezone="Australia/Melbourne"))
    reminder = real.reminders.create("bins out", datetime.now(UTC) + timedelta(hours=8))
    siris = _FakeSiris(
        {
            "home_assistant_list_entities": lambda a: [],
            "home_assistant_get_calendar_events": [
                {"summary": "Dentist", "start": f"{tomorrow}T15:00:00+10:00", "all_day": False},
                {"summary": "Standup", "start": f"{tomorrow}T09:30:00+10:00", "all_day": False},
                {"summary": "Today only", "start": "2000-01-01T09:00:00+10:00", "all_day": False},
            ],
            "weather_forecast": [
                {"conditions": "clear", "low_c": 9, "high_c": 18, "rain_chance_percent": 5},
                {"conditions": "slight rain", "low_c": 8, "high_c": 14, "rain_chance_percent": 80},
            ],
        },
        real,
    )
    try:
        tomorrow_info = BedtimeCheckSkill().run(siris)["tomorrow"]  # type: ignore[arg-type]
    finally:
        real.reminders.cancel(reminder["id"])
    assert tomorrow_info["first_event"] == "Standup (09:30)"
    assert tomorrow_info["weather"] == "slight rain, 8-14°C, 80% chance of rain"
    assert "bins out" in tomorrow_info["reminders"]


def test_a_failing_source_does_not_stop_the_rest() -> None:
    siris = _FakeSiris({"home_assistant_list_entities": _entities, "weather_forecast": RuntimeError("down")})
    result = BedtimeCheckSkill().run(siris)  # type: ignore[arg-type]
    assert result["lights_on"] == ["Lounge Lamps"] and "weather" not in result["tomorrow"]


def test_bedtime_is_registered_as_a_skill() -> None:
    siris = SirisAI(settings=Settings())
    assert siris.tools.get("bedtime_check") is not None


# -- briefing


def test_briefing_uses_ha_calendar_todo_forecast_and_todays_reminders_when_available() -> None:
    real = SirisAI(settings=Settings(timezone="Australia/Melbourne"))
    reminder = real.reminders.create("call the plumber", datetime.now(UTC) + timedelta(minutes=5))
    siris = _FakeSiris(
        {
            "weather_forecast": [{"conditions": "clear", "low_c": 9, "high_c": 18, "rain_chance_percent": 5}],
            "home_assistant_get_calendar_events": [{"summary": "Standup", "start": "2026-09-26T09:30:00+10:00"}],
            "home_assistant_get_todo_items": [{"list": "todo.shopping", "items": [{"summary": "milk"}, {"summary": "eggs"}]}],
            "server_health_check": {"warnings": []},
            "event_timing_check": {"warnings": []},
        },
        real,
    )
    siris.pending_notifications = real.pending_notifications  # type: ignore[attr-defined]
    siris.goals = real.goals  # type: ignore[attr-defined]
    try:
        briefing = DailyBriefingSkill().run(siris)  # type: ignore[arg-type]
    finally:
        real.reminders.cancel(reminder["id"])
    summary = briefing["summary"]
    assert "Forecast: clear, 9-18°C, 5% chance of rain." in summary
    assert "Standup" in summary and "To-do (2): milk, eggs." in summary
    assert "call the plumber" in summary
