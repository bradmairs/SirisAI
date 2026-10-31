"""The one health store (SirisAI 3.51): every metric with source priority,
workouts and sleep times, the Apple Health backfill, the overview and its
scores, exports and deletion, quick logs, the stale-import alert, insights,
and keeping health data off cloud models."""

import asyncio
import io
import zipfile
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.siris import SirisAI, get_siris
from app.core.user_context import set_current_user_id
from app.cortex.orchestrator import Cortex
from app.db.models import AttentionItem, HealthNote, HealthReading, HealthSample, HealthSleep, HealthWorkout
from app.db.session import SessionLocal
from app.health import apple_export, insights, overview, scores
from app.llm.base import LLMResponse, ToolCall
from app.main import app

USER = "health-store-user"
H = {"X-Siris-User": USER}


@pytest.fixture(autouse=True)
def _clean(client: TestClient):
    with SessionLocal() as session:
        for model in (HealthReading, HealthSample, HealthSleep, HealthWorkout, HealthNote):
            session.query(model).filter(model.user_id.in_([USER, "default"])).delete(synchronize_session=False)
        session.query(AttentionItem).delete()
        session.commit()
    s = SirisAI(settings=Settings())
    for key in (overview.IMPORTS_KEY, insights.INSIGHTS_KEY, insights.GOALS_KEY, insights.WIND_DOWN_KEY,
                f"health_readiness:{USER}", "health_readiness:default", f"health_intraday:{USER}", "health_intraday:default"):
        s.state.delete(key)
    set_current_user_id("default")
    yield
    app.dependency_overrides.pop(get_siris, None)
    set_current_user_id("default")


def _siris(**settings: Any) -> SirisAI:
    return SirisAI(settings=Settings(**{"timezone": "UTC", **settings}))


def _items(siris: SirisAI, source: str) -> dict[str, dict[str, Any]]:
    return {i["title"]: i for i in siris.attention.list() if i["source"] == source}


def test_the_best_source_wins_and_deleting_it_falls_back() -> None:
    siris = _siris()
    store = siris.health
    store.record({"steps": 7000}, "2026-10-01", USER, source="shortcut")
    store.record({"steps": 9000}, "2026-10-01", USER, source="health_auto_export")
    store.record({"steps": 1}, "2026-10-01", USER, source="manual")
    assert store.series("steps", "2026-10-01", USER) == [("2026-10-01", 9000.0)]
    store.delete("2026-10-01", "2026-10-01", source="health_auto_export", user_id=USER)
    assert store.series("steps", "2026-10-01", USER) == [("2026-10-01", 7000.0)]


def test_the_same_workout_from_two_sources_is_kept_once() -> None:
    store = _siris().health
    store.record_workouts({"2026-10-02": [{"name": "Running", "start": "2026-10-02 06:00:00 +1100", "minutes": 40, "km": 8}]}, USER, source="apple_export")
    store.record_workouts({"2026-10-02": [{"name": "Outdoor Run", "start": "2026-10-02 06:02:00 +1100", "minutes": 41, "km": 8.1}]}, USER, source="health_auto_export")
    store.record_workouts({"2026-10-02": [{"name": "Running", "start": "2026-10-02 06:00:00 +1100", "minutes": 40, "km": 8}]}, USER, source="apple_export")
    [w] = store.workouts("2026-10-01", USER)
    assert w["source"] == "health_auto_export" and w["km"] == 8.1


def test_imports_keep_everything_and_the_overview_scores_it(client: TestClient) -> None:
    days = {}
    base = date(2026, 10, 1)
    for i in range(20):
        d = (base + timedelta(days=i)).isoformat()
        days[d] = {"sleep_hours": 7.5, "heart_rate_variability": 60, "resting_heart_rate": 55, "exercise_minutes": 30,
                   "respiratory_rate": 14, "wrist_temperature": 0.0, "weight_kg": 80 - i * 0.05}
    native = {"metrics": days, "sleep": {"2026-10-20": {"start": "2026-10-19 22:40:00 +0000", "end": "2026-10-20 06:20:00 +0000"}},
              "workouts": {"2026-10-20": [{"name": "Outdoor Run", "start": "2026-10-20 07:00:00 +0000", "minutes": 30, "km": 6}]}}
    r = client.post("/siris/health/metrics", json=native, headers={**H, "X-Health-Source": "sirisos"})
    assert r.status_code == 200 and r.json()["source"] == "sirisos" and r.json()["workouts"] == 1
    siris = _siris()
    set_current_user_id(USER)
    built = overview.build(siris, date(2026, 10, 20))
    assert built["readiness"]["band"] in ("Primed", "Steady") and not built["readiness"]["early"]
    assert built["sleep"]["last_night"]["start"].startswith("2026-10-19 22:40")
    assert built["activity"]["workouts"][0]["km"] == 6
    assert built["weight"]["per_week"] < 0 and built["getting_sick"] is None
    assert overview.imports(siris)["sirisos"]["days"] == 20
    assert "Readiness" in overview.text(built)


def test_weight_trend_smooths_and_counts_down_to_a_goal() -> None:
    on = date(2026, 10, 30)
    days = {(on - timedelta(days=i)).isoformat(): {"weight_kg": 80 - (40 - i) * 0.07 + (0.8 if i == 3 else 0)} for i in range(40)}
    trend = scores.weight_trend(days, on, goal_kg=75)
    assert abs(trend["latest"] - trend["trend"]) < 1.5 and trend["per_week"] < 0
    assert trend["to_go_kg"] > 0 and trend["weeks_to_goal"] > 0


def test_getting_sick_needs_two_signs_two_days_running() -> None:
    on = date(2026, 10, 30)
    days = {(on - timedelta(days=i)).isoformat(): {"wrist_temperature": 0.0, "resting_heart_rate": 55, "heart_rate_variability": 60} for i in range(2, 30)}
    days[(on - timedelta(days=1)).isoformat()] = {"wrist_temperature": 0.7, "resting_heart_rate": 61, "heart_rate_variability": 58}
    days[on.isoformat()] = {"wrist_temperature": 0.6, "resting_heart_rate": 60, "heart_rate_variability": 45}
    found = scores.getting_sick(days, on)
    assert found and len(found["today"]) == 3 and len(found["yesterday"]) == 2
    days[(on - timedelta(days=1)).isoformat()]["resting_heart_rate"] = 55
    assert scores.getting_sick(days, on) is None


def _apple_zip() -> bytes:
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<HealthData>
 <Record type="HKQuantityTypeIdentifierStepCount" sourceName="iPhone" unit="count" startDate="2026-09-01 08:00:00 +1000" endDate="2026-09-01 09:00:00 +1000" value="5000"/>
 <Record type="HKQuantityTypeIdentifierStepCount" sourceName="Watch" unit="count" startDate="2026-09-01 08:00:00 +1000" endDate="2026-09-01 09:00:00 +1000" value="4000"/>
 <Record type="HKQuantityTypeIdentifierStepCount" sourceName="Watch" unit="count" startDate="2026-09-01 18:00:00 +1000" endDate="2026-09-01 19:00:00 +1000" value="2500"/>
 <Record type="HKQuantityTypeIdentifierRestingHeartRate" sourceName="Watch" unit="count/min" startDate="2026-09-01 07:00:00 +1000" endDate="2026-09-01 07:00:00 +1000" value="54"/>
 <Record type="HKQuantityTypeIdentifierBodyMass" sourceName="Scale" unit="kg" startDate="2026-09-01 07:00:00 +1000" endDate="2026-09-01 07:00:00 +1000" value="79.5"/>
 <Record type="HKQuantityTypeIdentifierDistanceWalkingRunning" sourceName="Watch" unit="m" startDate="2026-09-01 06:00:00 +1000" endDate="2026-09-01 06:30:00 +1000" value="5200"/>
 <Record type="HKCategoryTypeIdentifierSleepAnalysis" sourceName="Watch" startDate="2026-09-01 23:00:00 +1000" endDate="2026-09-02 01:00:00 +1000" value="HKCategoryValueSleepAnalysisAsleepCore"/>
 <Record type="HKCategoryTypeIdentifierSleepAnalysis" sourceName="Watch" startDate="2026-09-02 01:00:00 +1000" endDate="2026-09-02 02:30:00 +1000" value="HKCategoryValueSleepAnalysisAsleepDeep"/>
 <Record type="HKCategoryTypeIdentifierSleepAnalysis" sourceName="Watch" startDate="2026-09-02 02:30:00 +1000" endDate="2026-09-02 06:30:00 +1000" value="HKCategoryValueSleepAnalysisAsleepREM"/>
 <Record type="HKCategoryTypeIdentifierSleepAnalysis" sourceName="iPhone" startDate="2026-09-01 22:30:00 +1000" endDate="2026-09-02 06:45:00 +1000" value="HKCategoryValueSleepAnalysisInBed"/>
 <Workout workoutActivityType="HKWorkoutActivityTypeRunning" duration="30.5" durationUnit="min" totalDistance="5.2" totalDistanceUnit="km" totalEnergyBurned="350" totalEnergyBurnedUnit="kcal" startDate="2026-09-01 06:00:00 +1000" endDate="2026-09-01 06:30:30 +1000"/>
</HealthData>"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("apple_health_export/export.xml", xml)
    return buf.getvalue()


def test_apple_health_export_backfills_without_double_counting(client: TestClient) -> None:
    parsed = apple_export.parse_xml(io.BytesIO(zipfile.ZipFile(io.BytesIO(_apple_zip())).read("apple_health_export/export.xml")), since="2026-01-01")
    day = parsed["metrics"]["2026-09-01"]
    assert day["steps"] == 6500  # the watch's 6,500, not phone + watch
    assert day["resting_heart_rate"] == 54 and day["weight_kg"] == 79.5 and day["walk_run_km"] == 5.2 and day["running_km"] == 5.2
    night = parsed["metrics"]["2026-09-02"]
    assert night["sleep_hours"] == 7.5 and night["sleep_deep_h"] == 1.5 and night["sleep_rem_h"] == 4.0
    assert parsed["sleep"]["2026-09-02"]["start"].startswith("2026-09-01 23:00")
    assert parsed["workouts"]["2026-09-01"][0]["minutes"] == 30

    r = client.post("/siris/health/apple-export", files={"file": ("export.zip", _apple_zip(), "application/zip")}, params={"years": 20}, headers=H)
    assert r.status_code == 200, r.text
    assert r.json()["from"] == "2026-09-01" and r.json()["workouts"] == 1
    assert client.post("/siris/health/apple-export", files={"file": ("x.zip", b"nope", "application/zip")}, headers=H).status_code == 422


def test_export_report_delete_and_guest_mode(client: TestClient) -> None:
    from app.core import guest

    siris = _siris()
    app.dependency_overrides[get_siris] = lambda: siris
    siris.health.record({"steps": 8000, "resting_heart_rate": 55}, "2026-10-10", USER, source="shortcut")
    siris.health.add_note("symptom", "Sore throat", "2026-10-10", USER)
    csv = client.get("/siris/health/export.csv", params={"start": "2026-10-01", "end": "2026-10-31"}, headers=H)
    assert csv.status_code == 200 and "2026-10-10,steps,8000.0" in csv.text
    report = client.get("/siris/health/report", params={"days": 365}, headers=H)
    assert report.status_code == 200 and "Sore throat" in report.text and "Resting heart rate" in report.text
    gone = client.delete("/siris/health/data", params={"start": "2026-10-01", "end": "2026-10-31"}, headers=H)
    assert gone.json()["readings"] == 2 and siris.health.days("2026-10-01", user_id=USER) == {}
    guest.set_mode(siris, True, hours=1)
    try:
        assert client.get("/siris/health/export.csv", headers=H).status_code == 403
        assert client.get("/siris/hub/v1/health/overview", headers=H).json()["guest_mode"] is True
    finally:
        guest.set_mode(siris, False)


def test_quick_logs_meals_and_goals() -> None:
    siris = _siris()
    log = siris.tools.get("health_log")
    log.run(kind="water", amount=500)
    assert log.run(kind="water", amount=250)["today_total"] == 750
    assert log.run(kind="mood", amount=4)["today_total"] == 4
    assert log.run(kind="symptom", text="headache since lunch")["kind"] == "symptom"
    meal = siris.tools.get("health_log_meal").run(description="chicken salad", kcal=520, protein_g=38)
    assert meal["estimate"] and meal["today_kcal_estimate"] == 520
    with pytest.raises(ValueError):
        log.run(kind="water")
    today = overview.today(siris)
    siris.health.record({"running_km": 10}, today.isoformat(), source="health_auto_export")
    goal = siris.tools.get("health_set_goal").run(metric="running_km", target=1000, start=f"{today.year}-01-01", end=f"{today.year}-12-31")
    assert goal["done"] == 10 and goal["title"].startswith("1,000 km run")
    assert siris.tools.get("health_goals").run()[0]["goal_id"] == goal["goal_id"]
    assert siris.tools.get("health_overview").run()["text"]


def test_a_stalled_import_is_raised_and_clears() -> None:
    siris = _siris(health_stale_hours=36)
    siris.health.record({"steps": 1}, "2026-10-01", source="health_auto_export")
    assert overview.check_stale(siris, datetime.now(UTC) + timedelta(hours=40))
    assert "Health data hasn't arrived for 40 hours" in _items(siris, "health-import")
    assert overview.check_stale(siris) is None and not _items(siris, "health-import")


class FakeCalendar:
    def __init__(self, events: list[dict[str, Any]], writable: bool = True) -> None:
        self.events, self._writable, self.created = events, writable, []

    def writable(self) -> bool:
        return self._writable

    def events_between(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        return [e for e in self.events if start.isoformat()[:10] <= e["start"][:10] <= end.isoformat()[:10]]

    def create_event(self, summary: str, start: datetime, end: datetime, **kwargs: Any) -> dict[str, Any]:
        self.created.append((summary, start, end))
        return {"summary": summary}


def test_the_sick_warning_offers_to_block_the_evening() -> None:
    from app.hub import attention

    siris = _siris()
    on = date(2026, 10, 30)
    for i in range(2, 30):
        siris.health.record({"wrist_temperature": 0.0, "resting_heart_rate": 55, "heart_rate_variability": 60}, (on - timedelta(days=i)).isoformat(), source="health_auto_export")
    siris.health.record({"wrist_temperature": 0.7, "resting_heart_rate": 61, "heart_rate_variability": 50}, (on - timedelta(days=1)).isoformat(), source="health_auto_export")
    siris.health.record({"wrist_temperature": 0.6, "resting_heart_rate": 60, "heart_rate_variability": 45}, on.isoformat(), source="health_auto_export")
    siris.calendar = FakeCalendar([{"summary": "Dinner with Sam", "start": "2026-10-30T19:00:00+00:00", "end": "2026-10-30T21:00:00+00:00"}])  # type: ignore[assignment]
    assert insights.check_sick(siris, datetime(2026, 10, 30, 10, tzinfo=UTC))
    item = _items(siris, "health")["Your body may be fighting something off"]
    assert "Dinner with Sam at 19:00" in item["body"] and [a["id"] for a in item["actions"]] == ["block", "acknowledge"]
    attention.act(siris, item["id"], "block")
    assert siris.calendar.created[0][0] == "Rest (feeling unwell)"  # type: ignore[union-attr]


def test_sleep_insights_bedtime_and_wind_down() -> None:
    siris = _siris()
    on = date(2026, 10, 30)
    for i in range(1, 41):
        evening = on - timedelta(days=i)
        late = i % 3 == 0
        siris.health.record({"sleep_hours": 6.4 if late else 7.6}, (evening + timedelta(days=1)).isoformat(), source="health_auto_export")
        if late:
            siris.health.record_workouts({evening.isoformat(): [{"name": "Run", "start": f"{evening.isoformat()} 20:00:00 +0000", "minutes": 40}]}, source="health_auto_export")
        siris.health.record_sleep({(evening + timedelta(days=1)).isoformat(): {"start": f"{evening.isoformat()} 23:00:00 +0000", "end": f"{(evening + timedelta(days=1)).isoformat()} 07:00:00 +0000"}})
    found = insights.compute(siris, datetime(2026, 10, 30, 12, tzinfo=UTC))
    assert found[0]["factor"] == "late_workout" and found[0]["minutes"] == -72
    assert found[0]["text"].startswith("After a workout after 7pm you sleep 72 minutes less")

    siris.calendar = FakeCalendar([{"summary": "Site visit", "start": "2026-10-31T06:30:00+00:00", "end": "2026-10-31T08:00:00+00:00"}])  # type: ignore[assignment]
    plan = insights.bedtime(siris, datetime(2026, 10, 30, 18, tzinfo=UTC))
    assert plan["wake"] == "05:15" and "Site visit at 06:30" in plan["because"]
    bed = datetime.fromisoformat(plan["bedtime_at"])
    assert insights.wind_down_check(siris, datetime(2026, 10, 30, 18, tzinfo=UTC)) is None  # too early
    nudge_at = bed - timedelta(minutes=40)
    assert insights.wind_down_check(siris, nudge_at)["offered"]
    assert insights.wind_down_check(siris, nudge_at + timedelta(minutes=5)) is None  # once an evening
    assert "Time to wind down" in _items(siris, "health")


class CloudLLM:
    """A cloud model: asks for health_overview, and records what it was sent."""

    def __init__(self) -> None:
        self.calls: list[list[dict[str, Any]]] = []

    async def generate(self, messages: list[dict[str, Any]], *, tools: list[dict[str, Any]] | None = None, **kwargs: Any) -> LLMResponse:
        self.calls.append(messages)
        if tools:
            return LLMResponse(tool_calls=[ToolCall(name="health_overview", arguments={})])
        return LLMResponse(text="should not be asked")

    async def aclose(self) -> None:
        pass


def test_health_answers_never_reach_a_cloud_model() -> None:
    siris = _siris(llm_provider="anthropic", health_private=True)
    llm = CloudLLM()
    siris.llm = llm  # type: ignore[assignment]
    siris.health.record({"sleep_hours": 6.1, "resting_heart_rate": 77}, overview.today(siris).isoformat(), source="health_auto_export")
    conversation_id = siris.conversation.create_conversation()
    result = asyncio.run(Cortex().process(siris, conversation_id, "how did I sleep?", use_tools=True))
    assert len(llm.calls) == 1  # never asked to write the answer from the data
    assert "slept 6.1 hours" in result.response
    history = siris.conversation.get_messages(result.conversation_id)
    assert all("6.1" not in (m["content"] or "") for m in history if m["role"] in ("tool", "assistant"))
