"""Readiness from the night, kept once the morning's over, and the day's energy (3.62)."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.siris import SirisAI
from app.core.user_context import set_current_user_id
from app.db.models import HealthReading, HealthSample, HealthSleep, HealthWorkout
from app.db.session import SessionLocal
from app.health import auto_export, battery, overview, scores, strain

USER = "health-energy-user"
H = {"X-Siris-User": USER}
ON = date(2026, 10, 20)


@pytest.fixture(autouse=True)
def _clean(client: TestClient):
    with SessionLocal() as session:
        for model in (HealthReading, HealthSample, HealthSleep, HealthWorkout):
            session.query(model).filter(model.user_id == USER).delete(synchronize_session=False)
        session.commit()
    s = SirisAI(settings=Settings())
    for key in (f"health_readiness:{USER}", f"health_intraday:{USER}", overview.IMPORTS_KEY):
        s.state.delete(key)
    set_current_user_id(USER)
    yield
    set_current_user_id("default")


def _siris(**settings: Any) -> SirisAI:
    return SirisAI(settings=Settings(**{"timezone": "UTC", **settings}))


def _history(siris: SirisAI, nights: int = 14) -> None:
    for i in range(1, nights + 1):
        d = (ON - timedelta(days=i)).isoformat()
        siris.health.record({"sleep_hours": 7.5, "heart_rate_variability": 50, "hrv_overnight": 70, "resting_heart_rate": 55,
                             "exercise_minutes": 30}, d, USER, source="health_auto_export")


def _hae(hrv: list[tuple[str, float]], sleep: tuple[str, str] | None = ("2026-10-19 22:30:00 +1100", "2026-10-20 06:30:00 +1100"),
         hours: float = 7.6, extra: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    metrics: list[dict[str, Any]] = [{"name": "heart_rate_variability", "units": "ms", "data": [{"date": t, "qty": v} for t, v in hrv]}]
    if sleep:
        metrics.append({"name": "sleep_analysis", "units": "hr", "data": [
            {"date": "2026-10-20 00:00:00 +1100", "totalSleep": hours, "deep": 1.2, "rem": 1.6, "sleepStart": sleep[0], "sleepEnd": sleep[1]}]})
    return {"data": {"metrics": metrics + (extra or [])}}


def test_overnight_hrv_is_only_what_was_taken_asleep() -> None:
    parsed = auto_export.parse(_hae([("2026-10-19 23:30:00 +1100", 80), ("2026-10-20 03:00:00 +1100", 70),
                                     ("2026-10-20 17:00:00 +1100", 25)]))
    day = parsed["metrics"]["2026-10-20"]
    assert day["hrv_overnight"] == 75  # the 11:30pm reading belongs to the night that ended on the 20th
    assert parsed["intraday"]["2026-10-20"]["17"]["hrv"] == 25


def test_an_export_aggregated_by_day_has_no_overnight_or_hours() -> None:
    parsed = auto_export.parse(_hae([("2026-10-20 00:00:00 +1100", 60)]))
    assert "hrv_overnight" not in parsed["metrics"]["2026-10-20"] and parsed["intraday"] == {}


def test_readiness_uses_overnight_hrv_so_an_afternoon_workout_doesnt_move_it() -> None:
    siris = _siris()
    _history(siris)
    morning = auto_export.parse(_hae([("2026-10-20 03:00:00 +1100", 72)]))
    siris.health.store_parsed(morning, "health_auto_export", USER)
    first = scores.readiness(overview.days(siris, ON), ON)
    evening = auto_export.parse(_hae([("2026-10-20 03:00:00 +1100", 72), ("2026-10-20 16:00:00 +1100", 20), ("2026-10-20 17:00:00 +1100", 22)]))
    siris.health.store_parsed(evening, "health_auto_export", USER)
    later = scores.readiness(overview.days(siris, ON), ON)
    assert later["score"] == first["score"]
    assert any(c["text"].startswith("Overnight HRV 72") for c in later["contributors"])


def test_a_nap_or_a_partial_export_doesnt_replace_the_night() -> None:
    siris = _siris()
    siris.health.store_parsed(auto_export.parse(_hae([], hours=7.6)), "health_auto_export", USER)
    nap = auto_export.parse(_hae([], sleep=("2026-10-19 22:30:00 +1100", "2026-10-20 15:10:00 +1100"), hours=8.4))
    siris.health.store_parsed(nap, "health_auto_export", USER)
    tail = auto_export.parse(_hae([], sleep=("2026-10-20 03:00:00 +1100", "2026-10-20 06:30:00 +1100"), hours=3.5))
    siris.health.store_parsed(tail, "health_auto_export", USER)
    assert siris.health.days("2026-10-20")["2026-10-20"]["sleep_hours"] == 7.6
    # Sent mid-sleep first, then the whole night: the whole night wins.
    fuller = auto_export.parse(_hae([], sleep=("2026-10-19 22:30:00 +1100", "2026-10-20 07:15:00 +1100"), hours=8.2))
    siris.health.store_parsed(fuller, "health_auto_export", USER)
    assert siris.health.days("2026-10-20")["2026-10-20"]["sleep_hours"] == 8.2


def test_readiness_is_kept_once_the_morning_is_over() -> None:
    siris = _siris(health_readiness_lock_hour=10)
    _history(siris)
    siris.health.record({"sleep_hours": 7.5, "hrv_overnight": 72, "resting_heart_rate": 54}, ON.isoformat(), USER, source="health_auto_export")
    early = overview.morning_readiness(siris, ON, overview.days(siris, ON), datetime(2026, 10, 20, 8, 0))
    assert early["locked"] is False
    kept = overview.morning_readiness(siris, ON, overview.days(siris, ON), datetime(2026, 10, 20, 10, 5))
    assert kept["locked"] is True
    siris.health.record({"hrv_overnight": 30, "resting_heart_rate": 70}, ON.isoformat(), USER, source="health_auto_export")
    after = overview.morning_readiness(siris, ON, overview.days(siris, ON), datetime(2026, 10, 20, 18, 0))
    assert after["score"] == kept["score"]


def test_without_last_nights_sleep_it_waits_until_2pm() -> None:
    siris = _siris(health_readiness_lock_hour=10)
    _history(siris)
    days = overview.days(siris, ON)
    assert overview.morning_readiness(siris, ON, days, datetime(2026, 10, 20, 11, 0))["locked"] is False
    assert overview.morning_readiness(siris, ON, days, datetime(2026, 10, 20, 14, 0))["locked"] is True


def test_energy_starts_from_readiness_and_drains_with_a_run_and_stress() -> None:
    wake = datetime(2026, 10, 20, 6, 30)
    hours = {"07": {"kcal": 600, "hr": 150}, "10": {"kcal": 10, "hr": 85}, "11": {"kcal": 10, "hr": 85}, "13": {"kcal": 5, "hr": 57}}
    e = battery.compute({"score": 80}, hours, wake, datetime(2026, 10, 20, 14, 0), resting_hr=55)
    assert e["start"] == 85 and not e["estimated"]
    assert e["spent"]["activity"] >= 30 and e["spent"]["stress"] >= 5 and e["regained"] >= 1
    assert e["now"] < 45 and e["timeline"][0] == {"time": "06:30", "value": 85}
    calm = battery.compute({"score": 80}, {}, wake, datetime(2026, 10, 20, 14, 0), resting_hr=55)
    assert calm["now"] > e["now"]


def test_energy_is_estimated_from_the_days_totals_without_hours() -> None:
    e = battery.compute({"score": 60}, {}, datetime(2026, 10, 20, 7, 0), datetime(2026, 10, 20, 12, 0), 55,
                        workouts=[{"start": "2026-10-20 08:00:00 +1100", "kcal": 400}], day_kcal=600)
    assert e["estimated"] and e["start"] == 70 and e["spent"]["activity"] == 33


def test_hourly_buckets_from_the_import_feed_the_overview(client: TestClient) -> None:
    hours = [{"name": "active_energy", "units": "kcal", "data": [{"date": "2026-10-31 07:00:00 +0000", "qty": 500}]},
             {"name": "heart_rate", "units": "count/min", "data": [{"date": "2026-10-31 07:00:00 +0000", "Avg": 150, "Min": 90, "Max": 170}]}]
    r = client.post("/siris/health/metrics", json={"data": {"metrics": hours}}, headers=H)
    assert r.status_code == 200
    siris = _siris()
    assert battery.intraday(siris, "2026-10-31")["07"] == {"kcal": 500, "hr": 150}
    siris.health.record({"sleep_hours": 7.5, "hrv_overnight": 70}, "2026-10-31", USER, source="health_auto_export")
    built = overview.build(siris, now=datetime(2026, 10, 31, 9, 0))
    assert built["energy"]["spent"]["activity"] >= 27
    assert "Energy is at" in overview.text(built)


def test_history_compares_a_stretch_with_the_month_before() -> None:
    """3.93: 'how did I sleep the week before the deadline' answered from the store."""
    from app.plugins.builtin.health_data import HealthHistoryTool

    siris = _siris()
    for i in range(1, 41):
        d = (ON - timedelta(days=i)).isoformat()
        siris.health.record({"sleep_hours": 6.0 if i <= 7 else 7.5, "steps": 8000}, d, USER, source="health_auto_export")
    siris.state.set(f"health_readiness:{USER}", {(ON - timedelta(days=2)).isoformat(): {"score": 41}})
    out = HealthHistoryTool(siris).run(start=(ON - timedelta(days=7)).isoformat(), end=(ON - timedelta(days=1)).isoformat(), metrics="sleep_hours")
    sleep = out["metrics"]["sleep_hours"]
    assert sleep["average"] == 6.0 and sleep["usual_before"] == 7.5 and sleep["change_pct"] == -20.0 and sleep["days"] == 7
    assert "steps" not in out["metrics"] and out["readiness_by_day"] == {(ON - timedelta(days=2)).isoformat(): 41}
    from app.cortex.orchestrator import PRIVATE_TOOLS

    assert "health_history" in PRIVATE_TOOLS  # never sent to a cloud model


def test_strain_from_workouts_and_the_day_with_a_target() -> None:
    """4.03: an hour at 70-80% of max is about 14; the target follows readiness."""
    from app.health import strain

    assert strain.to_strain(60 * 3) == 13.9
    assert strain.zone_weight(150, 190) == 3 and strain.zone_weight(80, 190) == 0.5
    value, each = strain.day_strain([{"name": "Outdoor Run", "start": "2026-10-20 07:00:00 +1100", "minutes": 60, "avg_hr": 150}],
                                    {"07": {"hr": 150}, "12": {"hr": 120}}, 190)
    assert each[0]["strain"] == 13.9 and value > 13.9  # the hour of the run isn't counted twice; noon adds a little
    siris = _siris()
    siris.health.record_workouts({ON.isoformat(): [{"name": "Outdoor Run", "start": f"{ON.isoformat()} 07:00:00 +1100", "minutes": 30,
                                                    "avg_hr": 160, "max_hr": 182}]}, USER)
    built = strain.build(siris, {"band": "Recover"}, ON)
    assert built["target"] == [4, 9] and built["max_hr"] == 182 and built["status"] in ("in range", "over")
    assert [d["date"] for d in built["week"]][-1] == ON.isoformat()


def test_the_week_of_sleep_in_one_report() -> None:
    from app.health import scores

    days = {(ON - timedelta(days=i)).isoformat(): {"sleep_hours": 7.5} for i in range(8, 40)}
    for i, h in enumerate([6.0, 7.8, 5.5, 7.0, 6.2, 7.9, 6.4]):
        days[(ON - timedelta(days=i)).isoformat()] = {"sleep_hours": h}
    times = {(ON - timedelta(days=i)).isoformat(): {"start": f"{(ON - timedelta(days=i + 1)).isoformat()} {22 + i % 2}:{'00' if i % 3 else '45'}:00"}
             for i in range(7)}
    week = scores.sleep_week(days, times, ON, [{"text": "After a meeting past 8pm you sleep 38 minutes less."}])
    assert week["worst"]["hours"] == 5.5 and week["best"]["hours"] == 7.9 and week["nights"] == 7
    assert week["debt"] > 3 and week["tip"].startswith("You're") and week["factors"] == ["After a meeting past 8pm you sleep 38 minutes less."]


def test_the_setup_check_says_which_setting_to_change(client: TestClient, monkeypatch) -> None:
    """4.03: grouped by day, a partial day, and no workouts each name the fix.
    4.04: an export sent today that stops at yesterday ("Previous 7 Days") says to use Default."""
    from datetime import datetime as dt
    from zoneinfo import ZoneInfo

    sent = {"day": "2026-10-31"}
    monkeypatch.setattr(overview.clock, "now_local", lambda tz=None: dt.fromisoformat(f"{sent['day']}T15:00:00").replace(tzinfo=ZoneInfo("Australia/Melbourne")))
    by_day = {"data": {"metrics": [{"name": "step_count", "units": "count", "data": [{"date": "2026-10-30 00:00:00 +1100", "qty": 9000},
                                                                                    {"date": "2026-10-31 00:00:00 +1100", "qty": 3000}]}]}}
    assert client.post("/siris/health/metrics", json=by_day, headers=H).status_code == 200
    siris = _siris()
    check = overview.setup_check(siris)
    assert any("Aggregate" in p and "Hours" in p for p in check["problems"])
    assert any("Workouts" in p for p in check["problems"])
    partial = {"data": {"metrics": [{"name": "step_count", "units": "count", "data": [{"date": "2026-10-31 14:00:00 +1100", "qty": 300}]}],
                        "workouts": [{"name": "Outdoor Run", "start": "2026-10-31 07:00:00 +1100", "duration": 1800}]}}
    client.post("/siris/health/metrics", json=partial, headers=H)
    check = overview.setup_check(siris)
    assert any("part of today" in p and "Default" in p for p in check["problems"]) and not any("Aggregate" in p for p in check["problems"])
    assert not any("Workouts" in p for p in check["problems"])
    sent["day"] = "2026-11-01"  # the next morning, the export still ends on the 31st
    client.post("/siris/health/metrics", json=partial, headers=H)
    check = overview.setup_check(siris)
    assert any("stopped at yesterday" in p and "Default" in p for p in check["problems"])


def test_a_sirisgym_session_counts_in_strain_unless_the_watch_has_it() -> None:
    """4.06: SirisOS sends the day's gym sessions; one the Watch also recorded isn't counted twice."""
    gym = {"source": "sirisgym", "name": "SirisGYM: Push", "start": "2026-11-02 18:00:00 +1100", "minutes": 60}
    watch = {"source": "health_auto_export", "name": "Traditional Strength Training", "start": "2026-11-02 18:04:00 +1100", "minutes": 58, "avg_hr": 120}
    assert strain.without_doubles([gym, watch]) == [watch]
    assert strain.without_doubles([gym]) == [gym] and strain.workout_load(gym, 190) == 90
    native = {"metrics": {"2026-11-02": {}}, "workouts": {"2026-11-02": [{k: v for k, v in gym.items() if k != "source"}]}}
    assert auto_export.parse(native)["workouts"]["2026-11-02"][0]["minutes"] == 60


def test_gym_progress_answers_from_sirisgyms_summary(client: TestClient) -> None:
    """4.16: SirisOS posts a summary after each workout; the tool answers from it."""
    summary = {"total_sessions": 120, "streak_weeks": 6, "week": {"sessions": 2, "volume": 15200, "sets_by_muscle": {"chest": 12}},
               "muscles": [{"muscle": "back", "sets": 4, "target": [10, 20], "status": "under"}],
               "lifts": {"Bench Press": {"best": 116.7, "last": [{"date": "2026-10-01", "e1rm": 110.0, "top": 95}, {"date": "2026-10-29", "e1rm": 116.7, "top": 100}]}},
               "records_30d": ["Bench Press: heaviest ever, 100"], "deload": {"active": False, "suggest": False, "why": []},
               "next": {"routine": "Push, pull, legs", "day": "Push", "exercises": [{"name": "Bench Press", "sets": 3, "reps": "5-8", "weight": 102.5, "why": ""}]}}
    assert client.post("/siris/health/gym", json=summary, headers=H).json() == {"kept": True, "lifts": 1}
    siris = _siris()
    out = siris.run_tool("gym_progress", {"exercise": "bench"})
    assert out["text"].startswith("Bench Press: estimated 1RM 116.7 kg on 2026-10-29 (top set 100 kg), up 6.7 kg over 2 sessions")
    whole = siris.run_tool("gym_progress", {})["text"]
    assert "120 workouts logged, 6 weeks in a row." in whole and "Under 10-20 sets this week: back." in whole
    assert "Next: Push, pull, legs, Push: Bench Press 3×5-8 at 102.5 kg." in whole
    from app.cortex.orchestrator import PRIVATE_TOOLS
    from app.plugins.selector import ToolSelector
    assert "gym_progress" in PRIVATE_TOOLS
    specs = [{"type": "function", "function": {"name": f"tool_{i}", "description": "something else", "parameters": {}}} for i in range(40)]
    specs.append({"type": "function", "function": {"name": "gym_progress", "description": "strength training", "parameters": {}}})
    picked = ToolSelector(max_tools=8).select("how's my bench trending", specs)
    assert any((s.get("function") or s).get("name") == "gym_progress" for s in picked)
