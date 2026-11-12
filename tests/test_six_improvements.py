"""6.0: the nightly self-test, approvals from anywhere, and the two-machine split."""

from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core import daily_jobs
from app.core.config import Settings
from app.core.siris import SirisAI
from app.core.user_context import set_turn_role
from app.db.models import AttentionItem
from app.db.session import SessionLocal
from app.hub import attention
from app.notify import confirmation
from app.plugins.base import Tool, ToolPermission
from app.upkeep import self_test

NOW = datetime(2026, 11, 12, 17, 0, tzinfo=UTC)   # a Friday in Auckland, Thursday in UTC


@pytest.fixture(autouse=True)
def _clean(client: TestClient):
    with SessionLocal() as session:
        session.query(AttentionItem).filter(AttentionItem.key.like("self_test:%")).delete(synchronize_session=False)
        session.commit()
    yield
    set_turn_role("adult")


class _Weather:
    def __init__(self, temperature: float) -> None:
        self.temperature = temperature

    def current(self) -> dict:
        return {"temperature_c": self.temperature}


class _HomeAssistant:
    def __init__(self, state: str) -> None:
        self.state = state

    def list_states(self, domain: str | None = None) -> list[dict]:
        return [{"entity_id": "light.lounge", "state": self.state, "attributes": {"friendly_name": "Lounge light"}},
                {"entity_id": "light.dead", "state": "unavailable", "attributes": {"friendly_name": "Dead light"}}]

    def get_state_or_none(self, entity_id: str) -> dict | None:
        return {"entity_id": entity_id, "state": self.state}


def _siris(answers: dict[str, str], monkeypatch: pytest.MonkeyPatch, temperature: float = 14.2, light: str = "off") -> SirisAI:
    siris = SirisAI(settings=Settings(timezone="Pacific/Auckland"))
    siris._weather_client = _Weather(temperature)
    siris._home_assistant_client = _HomeAssistant(light)
    asked: list[str] = []

    async def ask(_siris: SirisAI, conversation_id: str, question: str) -> str:
        asked.append(question)
        assert conversation_id == "self-test-2026-11-12"
        return next(v for k, v in answers.items() if k in question.lower())

    monkeypatch.setattr(self_test, "_ask", ask)
    siris._asked = asked  # type: ignore[attr-defined]
    return siris


def _open(siris: SirisAI) -> list[dict[str, Any]]:
    return [i for i in siris.attention.list("open") if i["key"].startswith("self_test:")]


def test_right_answers_pass_and_raise_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    siris = _siris({"temperature": "It's 15 degrees and cloudy.", "lounge": "The Lounge light is off.", "day": "It's Friday."}, monkeypatch)
    result = self_test.run(siris, NOW)
    assert (result["passed"], result["failed"], result["skipped"]) == (3, 0, [])
    assert siris._asked[1] == "Is the Lounge light on or off right now?"   # never the unavailable one
    assert _open(siris) == []
    assert siris.state.get(self_test.RESULTS_KEY)["passed"] == 3


def test_a_wrong_answer_goes_to_the_feed_and_a_right_one_closes_it(monkeypatch: pytest.MonkeyPatch) -> None:
    siris = _siris({"temperature": "It's 21°C outside.", "lounge": "It's on.", "day": "Today is Thursday."}, monkeypatch, light="off")
    result = self_test.run(siris, NOW)
    assert result["failed"] == 3
    items = {i["key"]: i for i in _open(siris)}
    assert set(items) == {"self_test:weather", "self_test:home", "self_test:date"}
    assert "14.2 °C" in items["self_test:weather"]["body"] and "light.lounge is off" in items["self_test:home"]["body"]

    siris = _siris({"temperature": "13.5 degrees", "lounge": "Off.", "day": "Friday"}, monkeypatch, light="off")
    assert self_test.run(siris, NOW)["passed"] == 3
    assert _open(siris) == []


def test_missing_services_are_skipped_and_a_failed_turn_is_wrong(monkeypatch: pytest.MonkeyPatch) -> None:
    siris = SirisAI(settings=Settings(timezone="Pacific/Auckland"))
    siris._weather_client = None
    siris._home_assistant_client = None

    async def broken(*_args: Any) -> str:
        raise RuntimeError("model unreachable")

    monkeypatch.setattr(self_test, "_ask", broken)
    result = self_test.run(siris, NOW)
    assert result["skipped"] == ["weather", "home"] and result["failed"] == 1
    assert "model unreachable" in result["results"][0]["answer"]


class _Switch(Tool):
    name = "flip_switch"
    description = "Flips a switch."
    permission = ToolPermission.SAFE_ACTION
    parameters = {"type": "object", "properties": {}, "required": []}

    def run(self, **kwargs: Any) -> Any:
        return "flipped"


class _Look(_Switch):
    name = "look_at_switch"
    permission = ToolPermission.READ

    def run(self, **kwargs: Any) -> Any:
        return "it's off"


def test_a_self_test_turn_can_only_read() -> None:
    siris = SirisAI(settings=Settings())
    siris.tools.register(_Switch())
    siris.tools.register(_Look())
    set_turn_role("self_test")
    assert siris.run_tool("look_at_switch", {}) == "it's off"
    with pytest.raises(PermissionError, match="doesn't change anything"):
        siris.run_tool("flip_switch", {})
    set_turn_role("adult")
    assert siris.run_tool("flip_switch", {}) == "flipped"


def test_the_self_test_is_a_nightly_job() -> None:
    job = next(j for j in daily_jobs.JOBS if j.name == "self_test")
    assert job.hour(SirisAI(settings=Settings(llm_provider="ollama"))) == 4
    assert job.hour(SirisAI(settings=Settings(llm_provider="none"))) is None


# -- approvals from anywhere -------------------------------------------------


def test_every_approval_push_opens_in_sirisos() -> None:
    siris = SirisAI(settings=Settings(public_base_url="http://192.168.0.100:8000", sirisos_public_url="https://os.example/"))
    item = attention.raise_confirmation(siris, "chat", "home_assistant_call_service", {"domain": "light", "service": "turn_on"})
    buttons = confirmation.item_actions(siris, item)
    assert buttons[0] == {"action": "view", "label": "Open in SirisOS", "url": f"https://os.example/?attention={item['id']}", "clear": True}
    assert [b["label"] for b in buttons[1:]] == ["Approve", "Decline"]
    assert all(b["url"].startswith("http://192.168.0.100:8000/") for b in buttons[1:])


def test_sirisos_alone_still_gives_a_button() -> None:
    siris = SirisAI(settings=Settings(sirisos_public_url="https://os.example"))
    item = attention.raise_confirmation(siris, "chat", "home_assistant_call_service", {"domain": "light", "service": "turn_on"})
    assert [b["label"] for b in confirmation.item_actions(siris, item)] == ["Open in SirisOS"]
    assert confirmation.item_actions(SirisAI(settings=Settings()), item) is None


# -- two AI machines ---------------------------------------------------------


def test_embeddings_can_run_on_another_machine() -> None:
    siris = SirisAI(settings=Settings(embedding_provider="ollama", ollama_base_url="http://mac:11434", embedding_base_url="http://gpu:11434"))
    assert str(siris._embedding_client._client.base_url).rstrip("/") == "http://gpu:11434"
    same = SirisAI(settings=Settings(embedding_provider="ollama", ollama_base_url="http://mac:11434"))
    assert str(same._embedding_client._client.base_url).rstrip("/") == "http://mac:11434"


def test_twenty_tools_a_turn_by_default() -> None:
    # (The tests run with SIRISAI_TOOL_SELECTION_MAX=0, see conftest.py.)
    assert Settings.model_fields["tool_selection_max"].default == 20
    assert SirisAI(settings=Settings(tool_selection_max=20)).tool_selector.max_tools == 20
