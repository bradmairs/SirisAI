"""4.03: the morning brief spoken in the kitchen, once a day, when someone walks in."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.siris import SirisAI
from app.voice import morning


class FakeHA:
    def __init__(self, on_since: datetime) -> None:
        self.on_since = on_since
        self.calls: list[tuple] = []

    def get_state_or_none(self, entity_id: str) -> dict[str, Any] | None:
        return {"entity_id": entity_id, "state": "on", "last_changed": self.on_since.isoformat()}

    def call_service(self, domain: str, service: str, entity_id: str | None = None, **data: Any) -> None:
        self.calls.append((domain, service, entity_id, data))


@pytest.fixture
def siris(client: TestClient) -> SirisAI:
    s = SirisAI(settings=Settings(timezone="UTC", spoken_brief_trigger="binary_sensor.kitchen_motion",
                                  spoken_brief_player="media_player.kitchen", spoken_brief_tts="tts.piper"))
    s.state.delete(morning.DONE_KEY)
    return s


def test_spoken_once_when_someone_walks_in_in_the_morning(siris: SirisAI, monkeypatch) -> None:
    now = datetime(2026, 11, 2, 6, 40, tzinfo=UTC)
    ha = FakeHA(now - timedelta(seconds=30))
    siris._home_assistant_client = ha  # type: ignore[attr-defined]
    monkeypatch.setattr(morning, "text", lambda s: "Good morning. Nothing in the calendar.")
    said = morning.check(siris, now)
    assert said == {"spoken": "Good morning. Nothing in the calendar."}
    assert ha.calls == [("tts", "speak", "tts.piper", {"media_player_entity_id": "media_player.kitchen", "message": "Good morning. Nothing in the calendar."})]
    assert morning.check(siris, now + timedelta(minutes=5)) is None  # once a day
    assert len(ha.calls) == 1


def test_not_outside_the_window_or_for_a_sensor_stuck_on(siris: SirisAI, monkeypatch) -> None:
    monkeypatch.setattr(morning, "text", lambda s: "x")
    late = datetime(2026, 11, 2, 14, 0, tzinfo=UTC)
    siris._home_assistant_client = FakeHA(late)  # type: ignore[attr-defined]
    assert morning.check(siris, late) is None
    early = datetime(2026, 11, 2, 7, 0, tzinfo=UTC)
    siris._home_assistant_client = FakeHA(early - timedelta(hours=2))  # type: ignore[attr-defined]
    assert morning.check(siris, early) is None
    assert not morning.configured(SirisAI(settings=Settings()))


def test_the_words_hold_together_with_little_set_up(siris: SirisAI) -> None:
    said = morning.text(siris)
    assert said.startswith("Good morning.") and "None" not in said
