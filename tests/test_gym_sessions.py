"""Asking about SirisGym workouts (5.73): app/health/gym_sessions.py and the
gym_sessions tool, against a stand-in SirisOS and the workouts kept here."""

from __future__ import annotations

from datetime import date
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.siris import SirisAI
from app.health import gym_effort, gym_sessions
from app.sirisos.client import SirisOSClient

MONDAY = {
    "id": "s1", "title": "Legs", "started_at": "2026-11-09T07:00:00+11:00", "ended_at": "2026-11-09T08:05:00+11:00", "note": "Knee felt fine",
    "summary": {"minutes": 65},
    "entries": [
        {"exercise": "back-squat", "name": "Back Squat", "note": "", "sets": [
            {"weight": 60, "reps": 8, "type": "warmup", "done": True},
            {"weight": 120, "reps": 5, "type": "normal", "rpe": 8, "done": True},
            {"weight": 120, "reps": 5, "type": "normal", "done": True},
            {"weight": 125, "reps": 3, "type": "failure", "done": True},
        ]},
        {"exercise": "leg-press", "name": "Leg Press", "note": "seat 5", "sets": [{"weight": 200, "reps": 12, "done": True}, {"weight": 200, "reps": 10, "done": False}]},
    ],
    "records": [{"name": "Back Squat", "text": "Back Squat: heaviest ever, 125"}],
}
WEDNESDAY = {
    "id": "s2", "title": "Push", "started_at": "2026-11-11T07:00:00+11:00", "ended_at": "2026-11-11T07:50:00+11:00", "note": "", "records": [],
    "entries": [{"exercise": "bench-press", "name": "Bench Press", "note": "", "sets": [{"weight": 90, "reps": 6, "done": True}, {"weight": 90, "reps": 5, "type": "drop", "done": True}]}],
}
OCTOBER = {**WEDNESDAY, "id": "s0", "title": "Push", "started_at": "2026-10-28T07:00:00+11:00", "ended_at": "2026-10-28T07:45:00+11:00",
           "entries": [{"exercise": "bench-press", "name": "Bench Press", "note": "", "sets": [{"weight": 85, "reps": 6, "done": True}]}]}
FULL = {s["id"]: s for s in (MONDAY, WEDNESDAY, OCTOBER)}


def _summary(s: dict[str, Any]) -> dict[str, Any]:
    return {"id": s["id"], "title": s["title"], "date": s["started_at"][:10], "started_at": s["started_at"], "minutes": 60,
            "sets": sum(len(e["sets"]) for e in s["entries"]), "volume": 1000, "names": [e["name"] for e in s["entries"]]}


class FakeSirisOS:
    def __init__(self) -> None:
        self.paths: list[str] = []
        self.down = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.paths.append(str(request.url.path) + ("?" + request.url.query.decode() if request.url.query else ""))
        if self.down:
            return httpx.Response(503, json={"detail": "down"})
        assert request.headers["authorization"] == "Bearer service-key-service-key-123"
        path = request.url.path
        if path == "/api/v1/gym/sessions":
            month = request.url.params.get("month")
            picked = [_summary(s) for s in FULL.values() if s["started_at"].startswith(month or "")]
            return httpx.Response(200, json={"sessions": sorted(picked, key=lambda s: s["started_at"], reverse=True)})
        sid = path.rsplit("/", 1)[-1]
        assert request.url.params.get("watch") == "false"
        return httpx.Response(200, json=FULL[sid]) if sid in FULL else httpx.Response(404, json={"detail": "no such session"})


@pytest.fixture
def gym(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> tuple[SirisAI, FakeSirisOS]:
    siris = SirisAI(settings=Settings(timezone="Australia/Sydney"))
    fake = FakeSirisOS()
    siris._sirisos_client = SirisOSClient("http://sirisos", "service-key-service-key-123", transport=httpx.MockTransport(fake))
    monkeypatch.setattr(gym_sessions, "_today", lambda _s: date(2026, 11, 11))
    siris.state.delete(gym_effort._key("gym_sessions"))
    return siris, fake


def test_one_day_in_full_with_every_set(gym: tuple[SirisAI, FakeSirisOS]) -> None:
    siris, fake = gym
    out = siris.run_tool("gym_sessions", {"day": "monday"})
    assert out["source"] == "sirisos" and [s["id"] for s in out["sessions"]] == ["s1"]
    text = out["text"]
    assert text.startswith("Mon 9 Nov, Legs (65 min, 4 sets). Back Squat: 60 kg × 8 (warm-up), 120 kg × 5 @8, 120 kg × 5, 125 kg × 3 (to failure)")
    assert "Leg Press: 200 kg × 12 (note: seat 5)" in text and "200 kg × 10" not in text  # an unticked set isn't counted
    assert "Records: Back Squat: heaviest ever, 125." in text and "Note: Knee felt fine." in text
    assert "/api/v1/gym/sessions?month=2026-11" in fake.paths


def test_one_lift_across_workouts_with_its_best(gym: tuple[SirisAI, FakeSirisOS]) -> None:
    siris, _ = gym
    out = siris.run_tool("gym_sessions", {"exercise": "bench"})
    assert [s["date"] for s in out["sessions"]] == ["2026-11-11", "2026-10-28"]  # Legs has no bench: left out
    assert "Bench Press: 90 kg × 6, 90 kg × 5 (drop set)" in out["text"] and "Leg Press" not in out["text"]
    assert out["text"].endswith("Best estimated one-rep max in these: 108 kg.")


def test_a_range_lists_and_shows_the_newest(gym: tuple[SirisAI, FakeSirisOS]) -> None:
    siris, _ = gym
    out = siris.run_tool("gym_sessions", {"since": "2026-10-01"})
    assert out["count"] == 3 and out["text"].startswith("3 gym workouts from Thu 1 Oct to Wed 11 Nov: Wed 11 Nov, Push")
    assert "The latest in full: Wed 11 Nov, Push (50 min, 2 sets)" in out["text"]
    assert siris.run_tool("gym_sessions", {"day": "2026-11-10"})["text"] == "No gym workouts on Tue 10 Nov."
    assert siris.run_tool("gym_sessions", {"session_id": "s1"})["sessions"][0]["title"] == "Legs"
    with pytest.raises(ValueError):
        siris.run_tool("gym_sessions", {"day": "the other day"})


def test_without_sirisos_it_answers_from_the_workouts_kept_here(gym: tuple[SirisAI, FakeSirisOS]) -> None:
    siris, fake = gym
    fake.down = True
    gym_effort.keep_session(siris, {"id": "s1", "title": "Legs", "started_at": "2026-11-09T07:00:00+11:00", "ended_at": "2026-11-09T08:00:00+11:00",
                                    "sets": [{"exercise": "back-squat", "name": "Back Squat", "set": 1, "type": "normal", "weight": 120, "reps": 5, "at": "x"},
                                             {"exercise": "back-squat", "name": "Back Squat", "set": 2, "type": "normal", "weight": 120, "reps": 5, "at": "y"}]})
    out = siris.run_tool("gym_sessions", {"day": "2026-11-09"})
    assert out["source"] == "kept here"
    assert out["text"].startswith("Mon 9 Nov, Legs (60 min, 2 sets). Back Squat: 120 kg × 5, 120 kg × 5.")


def test_it_is_private_health_data(gym: tuple[SirisAI, FakeSirisOS]) -> None:
    from app.core import guest
    from app.cortex.orchestrator import PRIVATE_TOOLS

    siris, _ = gym
    assert "gym_sessions" in PRIVATE_TOOLS
    assert "gym_sessions" in siris.capabilities()["health"]
    assert guest.is_personal_tool("gym_sessions", "health")
