"""Return windows on things you bought (5.94)."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import pytest

from app.core.config import Settings
from app.core.siris import SirisAI
from app.llm.base import LLMClient, LLMResponse
from app.things import service as things


@pytest.fixture(autouse=True)
def _tables(client):  # type: ignore[no-untyped-def]
    """The app's start-up makes the tables (and adds return_until to an older database)."""
    yield


def test_a_receipt_that_states_a_return_window_sets_the_last_day() -> None:
    from app.documents import service as documents

    class Reader(LLMClient):
        async def generate(self, messages, *, tools=None, **kwargs) -> LLMResponse:  # type: ignore[no-untyped-def]
            return LLMResponse(text=json.dumps({"title": "JB Hi-Fi receipt", "sender": "JB Hi-Fi", "kind": "receipt", "summary": "", "due": [],
                                                "date": "2026-11-01", "return_days": 14,
                                                "items": [{"name": "Soundbar", "brand": "Sonos", "price": 699}]}))

    siris = SirisAI(settings=Settings(llm_provider="ollama", timezone="UTC"))
    siris.llm = Reader()
    doc = asyncio.run(documents.ingest(siris, "receipt.txt", "text/plain", b"Sonos soundbar $699. Change of mind returns within 14 days."))
    assert doc["things"][0]["return_until"] == "2026-11-15"


def test_nothing_is_assumed_when_the_receipt_doesnt_say() -> None:
    siris = SirisAI(settings=Settings(timezone="UTC"))
    kettle = things.add(siris, {"name": "Kettle", "bought": "2026-11-01"})
    assert kettle["return_until"] is None
    siris = SirisAI(settings=Settings(timezone="UTC", return_default_days=30))
    toaster = things.add(siris, {"name": "Toaster", "bought": "2026-11-01"})
    assert toaster["return_until"] == "2026-12-01"
    for t in (kettle, toaster):
        things.delete(siris, t["id"])


def test_the_last_days_to_return_it_are_flagged() -> None:
    siris = SirisAI(settings=Settings(timezone="UTC"))
    bar = things.add(siris, {"name": "soundbar", "bought": "2026-11-01", "return_days": 14, "shop": "JB Hi-Fi"})
    things.check(siris, datetime(2026, 11, 10, tzinfo=UTC))
    assert not any(i["key"] == f"return:{bar['id']}" for i in siris.attention.list())  # five days out: not yet
    things.check(siris, datetime(2026, 11, 14, tzinfo=UTC))
    item = next(i for i in siris.attention.list() if i["key"] == f"return:{bar['id']}")
    assert item["title"] == "Last day to return the soundbar: tomorrow" and "JB Hi-Fi" in item["body"]
    assert things.find(siris, "soundbar")[0]["return_until"] == "2026-11-15"
    things.delete(siris, bar["id"])


def test_a_stated_window_is_read_safely() -> None:
    assert things.return_days({"return_days": "30"}) == 30
    assert things.return_days({"return_days": 0}) is None
    assert things.return_days({"return_days": "a fortnight"}) is None
    assert things.return_days(None) is None
