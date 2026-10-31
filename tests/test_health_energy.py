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
from app.health import auto_export, battery, overview, scores

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
    for key in (f"health_readiness:{USER}", f"health_intraday:{USER}"):
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
