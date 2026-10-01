"""Health data from a phone (app/health/service.py, POST /siris/health/metrics)."""

from datetime import date, timedelta

from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.siris import SirisAI
from app.core.user_context import set_current_user_id
from app.health.service import HealthStore, normalize, summary
from app.db.session import SessionLocal

USER = {"X-Siris-User": "health-test-user"}


def test_normalize_accepts_aliases_and_reports_unknowns() -> None:
    metrics, ignored = normalize({"Sleep": "7.5", "step_count": 9000, "rhr": 58, "mood": "great", "hrv": "n/a"})
    assert metrics == {"sleep_hours": 7.5, "steps": 9000.0, "resting_heart_rate": 58.0}
    assert sorted(ignored) == ["hrv", "mood"]


def test_post_metrics_from_a_shortcut_and_upsert_per_day(client: TestClient) -> None:
    body = {"date": "2026-09-01", "metrics": {"sleep_hours": 6.5, "steps": 4000, "battery": 80}}
    response = client.post("/siris/health/metrics", json=body, headers=USER)
    assert response.status_code == 200
    assert response.json() == {"user_id": "health-test-user", "date": "2026-09-01", "stored": {"sleep_hours": 6.5, "steps": 4000.0}, "ignored": ["battery"]}
    # Same day again replaces rather than duplicates; a flat body works too.
    client.post("/siris/health/metrics", json={"date": "2026-09-01", "steps": 4500}, headers=USER)
    store = HealthStore(SessionLocal)
    assert store.series("steps", "2026-09-01", user_id="health-test-user")[:1] == [("2026-09-01", 4500.0)]


def test_bad_bodies_are_rejected(client: TestClient) -> None:
    assert client.post("/siris/health/metrics", json={"mood": "fine"}, headers=USER).status_code == 422
    assert client.post("/siris/health/metrics", json={"date": "yesterday", "steps": 1}, headers=USER).status_code == 422
    assert client.post("/siris/health/metrics", content=b"not json", headers=USER).status_code == 422


def test_summary_compares_with_your_own_baseline(client: TestClient) -> None:
    store = HealthStore(SessionLocal)
    user = "health-baseline-user"
    today = date(2026, 10, 1)
    for i in range(10, 0, -1):
        day = (today - timedelta(days=i)).isoformat()
        store.record({"sleep_hours": 7.4, "steps": 9000, "resting_heart_rate": 56}, day, user_id=user)
    store.record({"sleep_hours": 5.9, "steps": 8800, "resting_heart_rate": 63}, today.isoformat(), user_id=user)

    result = summary(store, today, user_id=user)
    assert result["metrics"]["sleep_hours"]["latest"] == 5.9
    assert result["metrics"]["sleep_hours"]["baseline"] == 7.4
    assert result["notes"] == [
        "slept 5.9h -- 1.5h under your usual",
        "resting heart rate 63 bpm -- 7.0 bpm over your usual",
    ]  # steps 200 under isn't notable


def test_health_summary_tool_and_briefing_line_are_per_user(client: TestClient) -> None:
    siris = SirisAI(settings=Settings(timezone="UTC"))
    from app.core import clock

    today = clock.now_local("UTC").date()
    siris.health.record({"sleep_hours": 7.0, "steps": 10234}, today.isoformat(), user_id="health-briefing-user")
    set_current_user_id("health-briefing-user")
    try:
        from app.skills.builtin.briefing_extras import briefing_lines

        assert "Health: slept 7.0h, 10,234 steps." in briefing_lines(siris)
        assert siris.run_tool("health_summary", {})["metrics"]["steps"]["latest"] == 10234
    finally:
        set_current_user_id("default")
    assert siris.run_tool("health_summary", {})["metrics"].get("steps", {}).get("latest") != 10234
