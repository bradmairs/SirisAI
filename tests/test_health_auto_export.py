"""Health Auto Export's JSON into POST /siris/health/metrics (3.11)."""

from app.health import auto_export

BODY = {"data": {
    "metrics": [
        {"name": "step_count", "units": "count", "data": [
            {"date": "2026-10-28 00:00:00 +1100", "qty": 5000, "source": "iPhone"},
            {"date": "2026-10-28 12:00:00 +1100", "qty": 3123},
            {"date": "2026-10-27 00:00:00 +1100", "qty": 9000}]},
        {"name": "resting_heart_rate", "units": "count/min", "data": [{"date": "2026-10-28 00:00:00 +1100", "qty": 56}, {"date": "2026-10-28 08:00:00 +1100", "qty": 58}]},
        {"name": "active_energy", "units": "kJ", "data": [{"date": "2026-10-28 00:00:00 +1100", "qty": 2092}]},
        {"name": "sleep_analysis", "units": "hr", "data": [{"date": "2026-10-28 00:00:00 +1100", "totalSleep": 7.2, "inBed": 7.9}]},
        {"name": "weight_body_mass", "units": "kg", "data": [{"date": "2026-10-28 07:00:00 +1100", "qty": 78.4}]},
        {"name": "walking_speed", "units": "km/hr", "data": [{"date": "2026-10-28", "qty": 5}]},
    ],
    "workouts": [
        {"name": "Outdoor Run", "start": "2026-10-28 06:10:00 +1100", "distance": {"qty": 8.2, "units": "km"}},
        {"name": "Outdoor Run", "start": "2026-10-28 18:00:00 +1100", "distance": {"qty": 1, "units": "mi"}},
        {"name": "Outdoor Walk", "start": "2026-10-28 12:00:00 +1100", "distance": {"qty": 3, "units": "km"}},
    ],
}}


def test_parses_days_and_combines_each_metric_its_own_way():
    days, ignored = auto_export.parse(BODY)
    assert days["2026-10-28"] == {"steps": 8123, "resting_heart_rate": 57, "active_energy_kcal": 500.0, "sleep_hours": 7.2,
                                  "weight_kg": 78.4, "running_km": 9.81}
    assert days["2026-10-27"] == {"steps": 9000}
    assert ignored == ["walking_speed"]


def test_the_route_takes_it(client):
    assert auto_export.looks_like(BODY) and not auto_export.looks_like({"steps": 3})
    r = client.post("/siris/health/metrics", json=BODY, headers={"X-Siris-User": "hae-test"})
    assert r.status_code == 200 and r.json()["format"] == "health_auto_export"
    assert sorted(r.json()["days"]) == ["2026-10-27", "2026-10-28"]
    assert client.post("/siris/health/metrics", json={"data": {"metrics": []}}, headers={"X-Siris-User": "hae-test"}).status_code == 422
