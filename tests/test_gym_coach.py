"""SirisGym's coach (4.29): advice after a workout, and the week's changes."""

from datetime import date, timedelta
from types import SimpleNamespace
from typing import Any
from unittest import mock

from app.health import gym_coach

ON = date(2026, 11, 5)


def _set(name: str, rec: int | None = None, rest: int | None = None, weight: float = 60, reps: int = 10, **kw: Any) -> dict:
    return {"exercise": name, "recovery_bpm": rec, "rest_s": rest, "weight": weight, "reps": reps, "type": "normal", **kw}


def test_session_advice_says_rest_longer_when_heart_rate_stays_up() -> None:
    advice = gym_coach.session_advice([_set("Bench Press", 10, 90), _set("Bench Press", 12, 90)], [])
    assert len(advice) == 1 and "only came down 11 bpm in about 90 s" in advice[0] and "30 s more rest" in advice[0]
    # A good recovery, a long rest, or one reading alone (a minute-at-a-time guess): nothing to say.
    assert gym_coach.session_advice([_set("Bench", 30, 90), _set("Bench", 28, 90)], []) == []
    assert gym_coach.session_advice([_set("Bench", 10, 200), _set("Bench", 10, 200)], []) == []
    assert gym_coach.session_advice([_set("Bench", 10, 90)], []) == []


def test_session_advice_sees_reps_fall_away_and_ignores_warmups() -> None:
    sets = [_set("Squat", reps=10, weight=100), _set("Squat", reps=6, weight=100)]
    assert "reps fell from 10 to 6 at 100 kg" in gym_coach.session_advice(sets, [])[0]
    assert gym_coach.session_advice([_set("Squat", reps=10, weight=100, type="warmup"), _set("Squat", reps=5, weight=100, type="warmup")], []) == []
    assert gym_coach.session_advice([_set("Squat", reps=10, weight=60), _set("Squat", reps=5, weight=100)], []) == []   # heavier: expected


def test_session_advice_notes_an_easy_lift_unless_rpe_says_otherwise() -> None:
    easy = {"exercise": "Curl", "effort": 3.0, "sets": 3, "rpe": None}
    assert "barely raised your heart rate" in gym_coach.session_advice([], [easy])[0]
    assert gym_coach.session_advice([], [{**easy, "rpe": 9.0}]) == []
    many = [_set(f"E{i}", 10, 90) for i in range(5) for _ in range(2)]
    assert len(gym_coach.session_advice(many, [])) == gym_coach.SESSION_ADVICE


def _weekly(metrics: dict[str, float] | None = None, n: int = 7, readiness: list[int] | None = None, summary: dict | None = None) -> dict:
    days = {(ON - timedelta(days=i)).isoformat(): dict(metrics or {}) for i in range(n)}
    with mock.patch.object(gym_coach.overview, "days", return_value=days), \
            mock.patch.object(gym_coach.overview, "recent_readiness", return_value=[{"score": s} for s in readiness or []]), \
            mock.patch.object(gym_coach.gym, "latest", return_value=summary):
        return gym_coach.weekly(SimpleNamespace(), ON)   # type: ignore[arg-type]


def test_weekly_says_when_it_cannot_tell_rather_than_guess() -> None:
    out = _weekly()
    assert out["changes"] == [] and out["enough"] is False and "Not enough to go on" in out["text"]


def test_weekly_puts_sleep_and_recovery_first_and_stops_at_two() -> None:
    out = _weekly({"sleep_hours": 5.5, "protein_g": 80, "weight_kg": 90}, readiness=[40, 45, 42], summary={"week": {"sessions": 2}, "muscles": [{"muscle": "legs", "status": "under"}]})
    assert [c["area"] for c in out["changes"]] == ["sleep", "recovery"]
    assert "5.5 h a night over 7 nights" in out["changes"][0]["why"]
    assert out["looked_at"] == ["sleep", "recovery", "protein", "training"]


def test_weekly_protein_uses_your_weight_and_training_comes_after() -> None:
    out = _weekly({"sleep_hours": 8, "protein_g": 80, "weight_kg": 90}, readiness=[80, 80, 80], summary={"week": {"sessions": 2}, "muscles": [{"muscle": "legs", "status": "under"}]})
    assert out["changes"][0]["title"] == "Add about 64 g of protein a day" and "144 g" in out["changes"][0]["why"]
    assert out["changes"][1]["title"] == "Add a few sets for legs"


def test_weekly_passes_on_siris_gyms_deload_but_not_twice() -> None:
    summary = {"week": {"sessions": 1}, "deload": {"suggest": True, "why": ["main lifts stalled for three sessions"]}}
    out = _weekly({"sleep_hours": 8}, readiness=[80, 80, 80], summary=summary)
    assert out["changes"][0] == {"area": "training", "title": "Take a deload week", "why": "Main lifts stalled for three sessions."}
    out = _weekly({"sleep_hours": 8}, readiness=[30, 30, 30], summary=summary)
    assert [c["title"] for c in out["changes"]] == ["Take a lighter week in the gym"]


def test_weekly_says_all_is_well_when_it_is() -> None:
    out = _weekly({"sleep_hours": 8, "protein_g": 170, "weight_kg": 90}, readiness=[80, 85, 80], summary={"week": {"sessions": 3}})
    assert out["changes"] == [] and out["enough"] is True and out["text"].startswith("Nothing to change this week")


def test_the_routes(client: Any) -> None:
    assert "changes" in client.get("/siris/health/gym/coach").json()


def test_the_brain_inbox_count_is_notes_and_memories(client: Any) -> None:
    from app.core.siris import get_siris
    from app.main import app

    brain = SimpleNamespace(inbox=lambda: {"notes": [{"title": "a"}, {"title": "b"}], "memory": [{"id": "x"}]})
    app.dependency_overrides[get_siris] = lambda: SimpleNamespace(second_brain=brain)
    try:
        assert client.get("/siris/brain/inbox/count").json() == {"count": 3}
    finally:
        app.dependency_overrides.pop(get_siris, None)
