"""4.31.0: "brief me" from a brief kept ready, no duplicate pushed cards, Home Assistant retries."""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.core import scheduler
from app.core.config import Settings
from app.core.siris import SirisAI
from app.cortex import quick
from app.cortex.orchestrator import Cortex
from app.db.models import AttentionItem
from app.db.session import SessionLocal
from app.home_assistant import client as ha_client
from app.hub import attention
from app.voice import brief_cache, morning


@pytest.fixture
def siris(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> SirisAI:
    s = SirisAI(settings=Settings(timezone="UTC", quick_commands=True))
    s.state.delete(brief_cache.KEY)
    built: list[int] = []

    def text(_s: SirisAI) -> str:
        built.append(1)
        return "Good morning. Today: sunny, up to 24 degrees. Nothing in the calendar."

    monkeypatch.setattr(morning, "text", text)
    s._built = built  # type: ignore[attr-defined]
    return s


def _at(monkeypatch: pytest.MonkeyPatch, hour: int) -> None:
    monkeypatch.setattr(brief_cache.clock, "now_local", lambda tz: datetime(2026, 11, 27, hour, 0))


@pytest.mark.parametrize("said", ["Brief me", "brief me.", "Hey Siris, brief me", "Give me a daily briefing", "give me my briefing",
                                  "morning briefing", "What's my day look like?", "how's my day looking"])
def test_brief_requests_are_quick(siris: SirisAI, said: str) -> None:
    planned = quick.plan(siris, said)
    assert planned is not None and planned.tool == "brief_me"


@pytest.mark.parametrize("said", ["brief me for tomorrow", "give me the full briefing and the news", "brief me on the SirisHydro project"])
def test_more_than_a_brief_goes_to_the_model(siris: SirisAI, said: str) -> None:
    assert quick.plan(siris, said) is None


def test_brief_me_reads_the_ready_brief_without_the_model(siris: SirisAI, monkeypatch: pytest.MonkeyPatch) -> None:
    _at(monkeypatch, 7)
    assert brief_cache.refresh(siris)["text"].startswith("Good morning. Today: sunny")
    assert len(siris._built) == 1

    class NoModel:
        async def generate(self, *a: Any, **k: Any) -> Any:
            raise AssertionError("the model was called")

        stream_generate = generate

    siris.llm = NoModel()  # type: ignore[assignment]
    result = asyncio.run(Cortex().process(siris, "brief-test", "Brief me"))
    assert result.response == "Good morning. Today: sunny, up to 24 degrees. Nothing in the calendar."
    assert len(siris._built) == 1   # read from the ready one, not built again


def test_a_stale_or_other_part_of_day_brief_is_rebuilt(siris: SirisAI, monkeypatch: pytest.MonkeyPatch) -> None:
    _at(monkeypatch, 11)
    brief_cache.refresh(siris)
    _at(monkeypatch, 13)   # morning's brief, now afternoon
    assert brief_cache.get(siris).startswith("Good afternoon. Today: sunny")
    assert len(siris._built) == 2
    _at(monkeypatch, 2)
    assert brief_cache.refresh(siris) is None   # not built overnight by the job...
    assert brief_cache.get(siris).startswith("Good morning.")   # ...but when asked


def test_the_ready_brief_is_a_scheduled_job() -> None:
    assert "brief_ready" in [j.name for j in scheduler._jobs(20)]


def test_brief_me_is_personal_for_guests(siris: SirisAI) -> None:
    from app.core import guest

    assert guest.is_personal_tool("brief_me", "")


def test_a_push_for_a_card_already_raised_isnt_a_second_card(client: TestClient) -> None:
    with SessionLocal() as session:
        session.query(AttentionItem).filter(AttentionItem.source == "health").delete(synchronize_session=False)
        session.commit()
    siris = SirisAI(settings=Settings())
    siris.attention.raise_item("health:wind-down:2026-11-27", "suggestion", "Time to wind down", source="health", severity="info", body="Bed at 10:30.")
    attention.on_push(siris, {"title": "Time to wind down", "message": "Bed at 10:30."}, "health: insight")
    assert [i["kind"] for i in siris.attention.list("open") if i["source"] == "health"] == ["suggestion"]
    attention.on_push(siris, {"title": "Sleep was short", "message": "5 h 10 min."}, "health: insight")
    assert sorted(i["kind"] for i in siris.attention.list("open") if i["source"] == "health") == ["alert", "suggestion"]


def test_home_assistant_retries_a_connection_it_couldnt_make() -> None:
    client = ha_client.HomeAssistantClient("http://ha.local:8123", "t")
    assert client._client._transport._pool._retries == ha_client.CONNECT_RETRIES == 3


def test_the_brief_says_since_last_night_after_noon_and_each_title_once(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import UTC

    from app.news import service as news

    s = SirisAI(settings=Settings(timezone="UTC"))
    monkeypatch.setattr(news, "overnight", lambda *_a: ["Time to wind down", "SirisAI: heading out", "Time to wind down"])
    monkeypatch.setattr(news, "stories", lambda *_a: [])
    monkeypatch.setattr(news, "episodes", lambda *_a: [])
    assert news.spoken(s, datetime(2026, 11, 27, 7, 0, tzinfo=UTC)) == "Overnight: Time to wind down; SirisAI: heading out."
    assert news.spoken(s, datetime(2026, 11, 27, 21, 0, tzinfo=UTC)) == "Since last night: Time to wind down; SirisAI: heading out."
