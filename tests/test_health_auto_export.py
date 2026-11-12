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
    parsed = auto_export.parse(BODY)
    days = parsed["metrics"]
    assert days["2026-10-28"] == {"steps": 8123, "resting_heart_rate": 57, "active_energy_kcal": 500.0, "sleep_hours": 7.2,
                                  "in_bed_h": 7.9, "weight_kg": 78.4, "running_km": 9.81}
    assert days["2026-10-27"] == {"steps": 9000}
    assert parsed["ignored"] == ["walking_speed"]
    assert [w["name"] for w in parsed["workouts"]["2026-10-28"]] == ["Outdoor Run", "Outdoor Run", "Outdoor Walk"]


def test_the_route_takes_it(client):
    assert auto_export.looks_like(BODY) and not auto_export.looks_like({"steps": 3})
    r = client.post("/siris/health/metrics", json=BODY, headers={"X-Siris-User": "hae-test"})
    assert r.status_code == 200 and r.json()["format"] == "health_auto_export"
    assert sorted(r.json()["days"]) == ["2026-10-27", "2026-10-28"]
    assert client.post("/siris/health/metrics", json={"data": {"metrics": []}}, headers={"X-Siris-User": "hae-test"}).status_code == 422


def test_sleep_grouped_by_hour_or_sent_as_stages_is_one_night_on_the_wake_up_day() -> None:
    """4.04: an hourly-grouped export splits the night at midnight, and an
    unsummarised one sends a segment per stage; both are one night."""
    z = " +1100"
    hourly = [{"date": "2026-10-31 23:00:00" + z, "asleep": 0.5, "core": 0.4, "deep": 0.1, "inBed": 0.5,
               "sleepStart": "2026-10-31 23:30:00" + z, "sleepEnd": "2026-11-01 00:00:00" + z},
              {"date": "2026-11-01 00:00:00" + z, "asleep": 6.5, "core": 4, "deep": 1, "rem": 1.5, "awake": 0.2, "inBed": 6.7,
               "sleepStart": "2026-11-01 00:00:00" + z, "sleepEnd": "2026-11-01 06:40:00" + z},
              {"date": "2026-11-01 14:00:00" + z, "asleep": 0.4, "sleepStart": "2026-11-01 14:00:00" + z, "sleepEnd": "2026-11-01 14:25:00" + z}]
    stages = [{"startDate": "2026-10-31 23:30:00" + z, "endDate": "2026-11-01 01:00:00" + z, "qty": 1.5, "value": "Core"},
              {"startDate": "2026-11-01 01:00:00" + z, "endDate": "2026-11-01 02:00:00" + z, "qty": 1, "value": "Deep"},
              {"startDate": "2026-11-01 02:00:00" + z, "endDate": "2026-11-01 02:10:00" + z, "qty": 0.17, "value": "Awake"},
              {"startDate": "2026-11-01 02:10:00" + z, "endDate": "2026-11-01 06:40:00" + z, "qty": 4.5, "value": "REM"}]
    for data, hours in ((hourly, 7.0), (stages, 7.0)):
        p = auto_export.parse({"data": {"metrics": [{"name": "sleep_analysis", "units": "hr", "data": data}]}})
        assert list(p["metrics"]) == ["2026-11-01"] and p["metrics"]["2026-11-01"]["sleep_hours"] == hours  # the nap isn't the night
        assert p["sleep"]["2026-11-01"] == {"start": "2026-10-31 23:30:00 +1100", "end": "2026-11-01 06:40:00 +1100"}


def test_state_of_mind_becomes_a_days_mood(client):
    body = {"data": {"stateOfMind": [
        {"start": "2026-11-10 08:00:00 +1100", "kind": "momentaryEmotion", "valence": 0.6, "labels": ["calm"]},
        {"start": "2026-11-10 21:00:00 +1100", "kind": "dailyMood", "valence": -0.2},
        {"start": "2026-11-11 07:30:00 +1100", "valenceClassification": "Very Unpleasant"},
        {"start": "2026-11-11 09:00:00 +1100"},  # nothing to go on
    ]}}
    assert auto_export.looks_like(body)
    days = auto_export.parse(body)["metrics"]
    assert days["2026-11-10"] == {"mood_valence": 0.2, "mood_entries": 2}
    assert days["2026-11-11"] == {"mood_valence": -0.86, "mood_entries": 1}
    r = client.post("/siris/health/metrics", json=body, headers={"X-Siris-User": "som-test"})
    assert r.status_code == 200 and r.json()["days"]["2026-11-10"]["mood_valence"] == 0.2
