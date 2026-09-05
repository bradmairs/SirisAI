from typing import Any

from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.siris import SirisAI, get_siris
from app.main import app
from app.skills.builtin.daily_briefing import DailyBriefingSkill


class _FakeTools:
    def __init__(
        self,
        has_weather: bool = False,
        has_calendar: bool = False,
        has_notify: bool = False,
        has_tautulli: bool = False,
        has_speak: bool = False,
    ) -> None:
        self._present = set()
        if has_weather:
            self._present.add("weather_current")
        if has_calendar:
            self._present.add("calendar_upcoming_events")
        if has_notify:
            self._present.add("notify")
        if has_tautulli:
            self._present.add("tautulli_recent_history")
        if has_speak:
            self._present.add("speak_in_current_room")

    def get(self, name: str) -> object | None:
        return object() if name in self._present else None


class _FakeGoals:
    def __init__(self, goals: list[dict[str, Any]] | None = None) -> None:
        self._goals = goals or []

    def list_goals(self, status: str | None = None) -> list[dict[str, Any]]:
        return [g for g in self._goals if status is None or g["status"] == status]


class _FakePendingNotifications:
    """Interruption controller (milestone 72): daily_briefing folds
    whatever's queued into its summary, then clears it -- `cleared`
    records whether that actually happened."""

    def __init__(self, pending: list[dict[str, Any]] | None = None) -> None:
        self._pending = pending or []
        self.cleared = False

    def list_pending(self) -> list[dict[str, Any]]:
        return list(self._pending)

    def clear(self) -> int:
        self.cleared = True
        count = len(self._pending)
        self._pending = []
        return count


class _FakeSirisForBriefing:
    def __init__(
        self,
        has_weather: bool = False,
        has_calendar: bool = False,
        has_notify: bool = False,
        has_tautulli: bool = False,
        has_speak: bool = False,
        weather_result: dict[str, Any] | None = None,
        calendar_result: list[dict[str, Any]] | None = None,
        tautulli_result: list[dict[str, Any]] | None = None,
        health_warnings: list[str] | None = None,
        timing_warnings: list[str] | None = None,
        goals: list[dict[str, Any]] | None = None,
        weather_raises: bool = False,
        calendar_raises: bool = False,
        tautulli_raises: bool = False,
        speak_sent: bool = True,
        pending_notifications: list[dict[str, Any]] | None = None,
    ) -> None:
        self.tools = _FakeTools(
            has_weather, has_calendar, has_notify, has_tautulli, has_speak
        )
        self.goals = _FakeGoals(goals)
        self.pending_notifications = _FakePendingNotifications(pending_notifications)
        self._weather_result = weather_result
        self._calendar_result = calendar_result if calendar_result is not None else []
        self._tautulli_result = tautulli_result if tautulli_result is not None else []
        self._health_warnings = health_warnings or []
        self._timing_warnings = timing_warnings or []
        self._weather_raises = weather_raises
        self._calendar_raises = calendar_raises
        self._tautulli_raises = tautulli_raises
        self._speak_sent = speak_sent
        self.notify_calls: list[dict[str, Any]] = []
        self.speak_calls: list[dict[str, Any]] = []

    def run_tool(self, name: str, arguments: dict[str, Any], **kwargs: Any) -> Any:
        if name == "weather_current":
            if self._weather_raises:
                raise ConnectionError("weather backend unreachable")
            return self._weather_result
        if name == "calendar_upcoming_events":
            if self._calendar_raises:
                raise ConnectionError("caldav server unreachable")
            return self._calendar_result
        if name == "tautulli_recent_history":
            if self._tautulli_raises:
                raise ConnectionError("tautulli unreachable")
            return self._tautulli_result
        if name == "server_health_check":
            return {
                "warnings": self._health_warnings,
                "healthy": not self._health_warnings,
            }
        if name == "event_timing_check":
            return {
                "warnings": self._timing_warnings,
                "healthy": not self._timing_warnings,
                "overdue_events": [],
            }
        if name == "notify":
            self.notify_calls.append(arguments)
            return {"sent": True}
        if name == "speak_in_current_room":
            self.speak_calls.append(arguments)
            return (
                {"sent": self._speak_sent}
                if self._speak_sent
                else {"sent": False, "reason": "no satellite currently known"}
            )
        if name == "daily_briefing":
            # POST /siris/proactive/briefing calls run_tool("daily_briefing", ...)
            # the same way it would for a real registered skill -- route it
            # through the actual skill logic (already covered by its own
            # direct tests above) rather than re-faking its composition here.
            return DailyBriefingSkill().run(self)
        raise AssertionError(f"unexpected tool call: {name}")


# -- DailyBriefingSkill


def test_briefing_omits_weather_and_calendar_when_not_configured() -> None:
    siris = _FakeSirisForBriefing()
    briefing = DailyBriefingSkill().run(siris)

    assert "weather" not in briefing
    assert "calendar" not in briefing
    assert briefing["warnings"] == []
    assert briefing["active_goals"] == []
    assert briefing["summary"] == "All systems normal. No active goals."


def test_briefing_includes_weather_when_configured() -> None:
    weather = {
        "temperature_c": 18.3,
        "humidity_percent": 62,
        "wind_speed_kmh": 12.4,
        "precipitation_mm": 0.0,
        "conditions": "overcast",
    }
    siris = _FakeSirisForBriefing(has_weather=True, weather_result=weather)
    briefing = DailyBriefingSkill().run(siris)

    assert briefing["weather"] == weather
    assert "18.3°C, overcast" in briefing["summary"]


def test_briefing_survives_weather_being_unreachable() -> None:
    """Caught live: the first pass let a real network failure fetching
    weather 500 the entire endpoint, losing warnings/goals that had
    nothing to do with weather. Weather is enrichment, not the point of
    the briefing -- same best-effort precedent as
    DockerRestartContainerTool.dry_run()'s Plex session check."""
    siris = _FakeSirisForBriefing(
        has_weather=True,
        weather_raises=True,
        goals=[{"title": "Ship it", "status": "active"}],
    )
    briefing = DailyBriefingSkill().run(siris)

    assert "weather" not in briefing
    assert briefing["active_goals"] == ["Ship it"]
    assert "Ship it" in briefing["summary"]


def test_briefing_survives_calendar_being_unreachable() -> None:
    siris = _FakeSirisForBriefing(has_calendar=True, calendar_raises=True)
    briefing = DailyBriefingSkill().run(siris)

    assert "calendar" not in briefing
    assert briefing["summary"]  # still produced a summary despite the failure


def test_briefing_lists_todays_events_when_calendar_configured() -> None:
    events = [
        {
            "summary": "Standup",
            "start": "2026-01-10T10:00:00",
            "end": None,
            "location": None,
            "calendar": "Work",
        }
    ]
    siris = _FakeSirisForBriefing(has_calendar=True, calendar_result=events)
    briefing = DailyBriefingSkill().run(siris)

    assert briefing["calendar"] == events
    assert "Standup" in briefing["summary"]


def test_briefing_reports_nothing_on_calendar_when_empty() -> None:
    siris = _FakeSirisForBriefing(has_calendar=True, calendar_result=[])
    briefing = DailyBriefingSkill().run(siris)
    assert "Nothing on the calendar today." in briefing["summary"]


def test_briefing_merges_health_and_event_timing_warnings() -> None:
    siris = _FakeSirisForBriefing(
        health_warnings=["CPU usage is high: 95%"],
        timing_warnings=["Expected recurring event is overdue: 'backup'"],
    )
    briefing = DailyBriefingSkill().run(siris)

    assert briefing["warnings"] == [
        "CPU usage is high: 95%",
        "Expected recurring event is overdue: 'backup'",
    ]
    assert "2 warning(s)" in briefing["summary"]


def test_briefing_includes_recent_plex_activity_when_configured() -> None:
    activity = [{"title": "Inception", "user": "brad", "date": "2026-01-01"}]
    siris = _FakeSirisForBriefing(has_tautulli=True, tautulli_result=activity)
    briefing = DailyBriefingSkill().run(siris)

    assert briefing["plex_recent_activity"] == activity
    assert "Recently watched: Inception (brad)." in briefing["summary"]


def test_briefing_reports_nothing_watched_when_tautulli_history_is_empty() -> None:
    siris = _FakeSirisForBriefing(has_tautulli=True, tautulli_result=[])
    briefing = DailyBriefingSkill().run(siris)
    assert "Nothing recently watched." in briefing["summary"]


def test_briefing_survives_tautulli_being_unreachable() -> None:
    siris = _FakeSirisForBriefing(has_tautulli=True, tautulli_raises=True)
    briefing = DailyBriefingSkill().run(siris)

    assert "plex_recent_activity" not in briefing
    assert briefing["summary"]  # still produced a summary despite the failure


def test_briefing_omits_plex_activity_when_tautulli_not_configured() -> None:
    siris = _FakeSirisForBriefing()
    briefing = DailyBriefingSkill().run(siris)
    assert "plex_recent_activity" not in briefing


def test_briefing_lists_active_goals() -> None:
    goals = [
        {"title": "Reduce power consumption", "status": "active"},
        {"title": "Old goal", "status": "completed"},
    ]
    siris = _FakeSirisForBriefing(goals=goals)
    briefing = DailyBriefingSkill().run(siris)

    assert briefing["active_goals"] == ["Reduce power consumption"]
    assert "Active goals (1): Reduce power consumption." in briefing["summary"]


def test_briefing_folds_in_and_clears_queued_notifications() -> None:
    pending = [
        {
            "message": "CPU usage is high: 95%",
            "severity": "warning",
            "category": "resource",
            "created_at": "x",
        },
        {
            "message": "Unrecognized device connected: foo",
            "severity": "warning",
            "category": "network",
            "created_at": "x",
        },
    ]
    siris = _FakeSirisForBriefing(pending_notifications=pending)
    briefing = DailyBriefingSkill().run(siris)

    assert briefing["queued_notifications"] == [
        "CPU usage is high: 95%",
        "Unrecognized device connected: foo",
    ]
    assert (
        "Also since last time: CPU usage is high: 95%; Unrecognized device connected: foo."
        in briefing["summary"]
    )
    assert siris.pending_notifications.cleared is True
    assert siris.pending_notifications.list_pending() == []


def test_briefing_omits_queued_notifications_line_when_none_pending() -> None:
    siris = _FakeSirisForBriefing()
    briefing = DailyBriefingSkill().run(siris)

    assert briefing["queued_notifications"] == []
    assert "Also since last time" not in briefing["summary"]
    assert siris.pending_notifications.cleared is False


def test_daily_briefing_tool_is_registered_as_a_skill() -> None:
    siris = SirisAI(settings=Settings())
    assert siris.tools.get("daily_briefing") is not None
    assert "daily_briefing" in siris.capabilities()["skills"]


# -- Endpoint-level: POST /siris/proactive/briefing


def test_briefing_endpoint_notifies_when_channel_configured(client: TestClient) -> None:
    fake_siris = _FakeSirisForBriefing(
        has_notify=True, goals=[{"title": "Ship SirisAI", "status": "active"}]
    )
    app.dependency_overrides[get_siris] = lambda: fake_siris
    try:
        response = client.post("/siris/proactive/briefing")
        assert response.status_code == 200
        body = response.json()
        assert body["notified"] is True
        assert body["result"]["active_goals"] == ["Ship SirisAI"]
        assert fake_siris.notify_calls == [
            {"title": "SirisAI: daily briefing", "message": body["result"]["summary"]}
        ]
    finally:
        app.dependency_overrides.pop(get_siris, None)


def test_briefing_endpoint_does_not_notify_without_a_configured_channel(
    client: TestClient,
) -> None:
    fake_siris = _FakeSirisForBriefing(has_notify=False)
    app.dependency_overrides[get_siris] = lambda: fake_siris
    try:
        response = client.post("/siris/proactive/briefing")
        assert response.status_code == 200
        assert response.json()["notified"] is False
    finally:
        app.dependency_overrides.pop(get_siris, None)


def test_briefing_endpoint_speaks_when_a_satellite_is_known(client: TestClient) -> None:
    fake_siris = _FakeSirisForBriefing(
        has_speak=True,
        speak_sent=True,
        goals=[{"title": "Ship SirisAI", "status": "active"}],
    )
    app.dependency_overrides[get_siris] = lambda: fake_siris
    try:
        response = client.post("/siris/proactive/briefing")
        assert response.status_code == 200
        body = response.json()
        assert body["spoken"] is True
        assert fake_siris.speak_calls == [{"message": body["result"]["summary"]}]
    finally:
        app.dependency_overrides.pop(get_siris, None)


def test_briefing_endpoint_reports_not_spoken_when_no_satellite_is_known(
    client: TestClient,
) -> None:
    """speak_in_current_room returns sent: False (not an error) when
    nobody's home to hear it -- the endpoint must still succeed and
    honestly report spoken: False, not just "attempted"."""
    fake_siris = _FakeSirisForBriefing(has_speak=True, speak_sent=False)
    app.dependency_overrides[get_siris] = lambda: fake_siris
    try:
        response = client.post("/siris/proactive/briefing")
        assert response.status_code == 200
        assert response.json()["spoken"] is False
    finally:
        app.dependency_overrides.pop(get_siris, None)


def test_briefing_endpoint_does_not_speak_without_a_known_satellite_tool(
    client: TestClient,
) -> None:
    fake_siris = _FakeSirisForBriefing(has_speak=False)
    app.dependency_overrides[get_siris] = lambda: fake_siris
    try:
        response = client.post("/siris/proactive/briefing")
        assert response.status_code == 200
        assert response.json()["spoken"] is False
        assert fake_siris.speak_calls == []
    finally:
        app.dependency_overrides.pop(get_siris, None)


def test_briefing_endpoint_real_end_to_end_without_notify_configured(
    client: TestClient,
) -> None:
    # Default test settings have no ntfy topic and no weather/calendar --
    # should still succeed with a minimal briefing, just never notify.
    response = client.post("/siris/proactive/briefing")
    assert response.status_code == 200
    body = response.json()
    assert "summary" in body["result"]
    assert body["notified"] is False
