"""7.28: the doctor's summary month by month, and what's going the wrong way."""

from __future__ import annotations

from datetime import date, timedelta

from app.health import report

ON = date(2026, 11, 19)


def _days(n: int = 90, recent: dict | None = None, before: dict | None = None) -> dict[str, dict]:
    out = {}
    for i in range(n):
        d = ON - timedelta(days=i)
        is_recent = i < 30
        m = {"sleep_hours": 7.4, "resting_heart_rate": 56, "heart_rate_variability": 60, "weight_kg": 82.0, "steps": 9000,
             "exercise_minutes": 40, "mood_valence": 0.3, "alcohol_drinks": 1, "blood_oxygen": 97}
        m |= (recent if is_recent else before) or {}
        out[d.isoformat()] = m
    return out


def test_nothing_stands_out_when_steady():
    assert report.worth_mentioning(_days(), ON) == []


def test_wrong_way_trends_are_named():
    flags = report.worth_mentioning(_days(recent={"sleep_hours": 6.6, "resting_heart_rate": 61, "heart_rate_variability": 48, "weight_kg": 84.5,
                                                  "steps": 5000, "mood_valence": -0.1, "alcohol_drinks": 2, "blood_oxygen": 94}), ON)
    text = " ".join(flags)
    assert "Sleep is down: 6.6 h" in text and "Resting heart rate is up: 61 bpm" in text and "HRV is down 20%" in text
    assert "Weight is up 2.5 kg" in text and "Less active: 5,000 steps" in text and "Mood is lower" in text
    assert "Blood oxygen has averaged 94%" in text and "About 14 drinks a week" in text


def test_too_few_days_says_nothing():
    data = {k: v for k, v in _days(recent={"sleep_hours": 5.0}).items() if k > (ON - timedelta(days=35)).isoformat()}
    flags = report.worth_mentioning(data, ON)
    assert not any(f.startswith("Sleep is down") for f in flags)  # only 5 days before the last month
    assert any(f.startswith("Sleep has averaged 5.0 h") for f in flags)


def test_month_by_month():
    ms, rows = report.months(_days(), ON, 6)
    assert ms == ["2026-08", "2026-09", "2026-10", "2026-11"]
    by = dict(rows)
    assert by["Sleep"] == ["7.4 h"] * 4 and by["Mood"] == ["fairly good"] * 4 and by["Drinks a week"] == ["7.0"] * 4
