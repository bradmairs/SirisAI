"""7.6: long conversations, "tell me when", memory tidy-up, the door, the setup check,
can't-do suggestions, and meals."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.siris import get_siris
from app.hub.attention import act
from app.llm.base import LLMResponse


# -- long conversations ------------------------------------------------------

def _chat(n: int, big: int = 0) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for i in range(n):
        out.append({"role": "user", "content": f"question {i}"})
        if big and i % 2 == 0:
            out.append({"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "docker_list", "arguments": {}}}]})
            out.append({"role": "tool", "content": "x" * big})
        out.append({"role": "assistant", "content": f"answer {i}"})
    return out


def test_history_keeps_recent_turns_and_summarises_the_rest(client: TestClient) -> None:
    from app.cortex import history

    siris = get_siris()
    messages = _chat(20, big=5000)
    kept = history.compact(siris, "conv-hist", messages)
    assert kept[0]["role"] == "system" and kept[0]["content"].startswith("Earlier in this conversation")
    assert "You were asked: question 0" in kept[0]["content"] or "question" in kept[0]["content"]
    assert kept[-1] == {"role": "assistant", "content": "answer 19"}
    assert kept[1]["role"] != "tool"                                   # never a result without its call
    assert len(kept) <= siris.settings.history_keep_messages + 1
    assert sum(len(str(m.get("content") or "")) for m in kept) < siris.settings.history_max_chars + 2000
    tools = [m for m in kept if m["role"] == "tool"]
    assert all(len(m["content"]) < 2000 for m in tools[:-1])           # older results cut down


def test_a_short_conversation_is_untouched(client: TestClient) -> None:
    from app.cortex import history

    messages = _chat(3)
    assert history.compact(get_siris(), "conv-short", messages) == messages


def test_the_model_summary_is_used_and_refreshed(client: TestClient, monkeypatch: Any) -> None:
    from app.cortex import history

    class Summariser:
        async def generate(self, *a: Any, **k: Any) -> LLMResponse:
            return LLMResponse(text="Brad asked about the NAS; disks are fine.")

    siris = get_siris()
    monkeypatch.setattr(siris, "llm", Summariser())
    messages = _chat(20)
    siris.state.delete(history.SUMMARY_KEY.format("conv-sum"))
    assert asyncio.run(history.refresh_summary(siris, "conv-sum", messages)) == "Brad asked about the NAS; disks are fine."
    kept = history.compact(siris, "conv-sum", messages)
    assert "disks are fine" in kept[0]["content"]
    assert asyncio.run(history.refresh_summary(siris, "conv-sum", messages)) is None   # nothing new to summarise


def test_the_context_window_is_sent_to_ollama() -> None:
    from app.llm.ollama import OllamaClient

    assert OllamaClient("http://o", "m", num_ctx=8192)._keep()["options"] == {"num_ctx": 8192}
    assert "options" not in OllamaClient("http://o", "m")._keep()
    assert Settings().ollama_num_ctx == 8192


# -- tell me when -------------------------------------------------------------

def test_watch_conditions() -> None:
    from app.reminders.watches import holds

    assert holds({"text_contains": "Dune"}, [{"title": "Dune"}])[0]
    assert holds({"text_missing": "slow horses"}, [{"title": "Other"}])[0]
    assert holds({"number_at": "0.free_gb", "below": 100}, [{"free_gb": 80}]) == (True, "it's 80")
    assert not holds({"number_at": "0.free_gb", "below": 100}, [{"free_gb": 250}])[0]
    assert holds({"above": 30}, {"temperature_c": 31.5})[0]


def test_a_watch_is_made_checked_and_fires_once(client: TestClient, monkeypatch: Any) -> None:
    from app.reminders import watches

    siris = get_siris()
    siris.state.delete(watches.STATE_KEY)
    results = {"value": [{"title": "Other film"}]}
    real = siris.run_tool

    def run_tool(name: str, arguments: dict[str, Any], **kw: Any) -> Any:
        return results["value"] if name == "get_current_time" else real(name, arguments, **kw)

    monkeypatch.setattr(siris, "run_tool", run_tool)
    made = watches.create(siris, "Dune is on Plex", "get_current_time", {}, text_contains="dune")
    assert made["watching"] and made["every_minutes"] == 15
    now = datetime.now(UTC)
    assert watches.check(siris, now + timedelta(minutes=5)) == []           # not due yet
    assert watches.check(siris, now + timedelta(minutes=16)) == []          # due, not true
    results["value"] = [{"title": "Dune: Part Two"}]
    fired = watches.check(siris, now + timedelta(minutes=40))
    assert [f["what"] for f in fired] == ["Dune is on Plex"] and siris.state.get(watches.STATE_KEY) == []
    assert watches.create(siris, "Dune", "get_current_time", {}, text_contains="dune")["already"]   # true already: told now


def test_watches_only_look_things_up(client: TestClient) -> None:
    from app.reminders import watches

    with pytest.raises(ValueError, match="changes things"):
        watches.create(get_siris(), "x", "remember", {"content": "x"}, text_contains="y")
    with pytest.raises(ValueError, match="what to wait for"):
        watches.create(get_siris(), "x", "get_current_time", {})


# -- memory tidy-up -----------------------------------------------------------

def test_stale_memories() -> None:
    from app.memory.tidy import stale

    now = datetime(2026, 11, 21)
    old = (now - timedelta(days=60)).isoformat()
    memories = [
        {"id": 1, "content": "Brad's car is in for service this week", "created_at": old, "last_recalled_at": None},
        {"id": 2, "content": "Brad likes flat whites", "created_at": old, "last_recalled_at": old},
        {"id": 3, "content": "Dentist on 2026-10-01", "created_at": old, "last_recalled_at": None},
        {"id": 4, "content": "Brad's sister is Kate", "created_at": (now - timedelta(days=400)).isoformat(), "last_recalled_at": None},
    ]
    assert [m["id"] for m in stale(memories, now)] == [1, 3, 4]


def test_the_monthly_tidy_up_cards(client: TestClient, monkeypatch: Any) -> None:
    from app.memory import tidy

    class Judge:
        async def generate(self, *a: Any, **k: Any) -> LLMResponse:
            return LLMResponse(text="[]")

    siris = get_siris()
    monkeypatch.setattr(siris, "llm", Judge())
    stale_one = siris.memory.remember("Brad is currently staying in Sydney")
    monkeypatch.setattr(tidy, "stale", lambda memories, now: [{**m, "why": "test"} for m in memories if m["id"] == stale_one["id"]])
    out = tidy.review(siris)
    assert out["stale"] == 1
    item = next(i for i in siris.attention.list("open") if i["key"] == f"memory_tidy:stale:{stale_one['id']}")
    act(siris, item["id"], "forget")
    assert all(m["id"] != stale_one["id"] for m in siris.memory.list_all())


# -- the door -----------------------------------------------------------------

def test_someone_at_the_door(client: TestClient, monkeypatch: Any) -> None:
    from app.household import door

    class Frigate:
        def __init__(self) -> None:
            self.events: list[dict[str, Any]] = []

        def recent_events(self, label: str = "person", limit: int = 15) -> list[dict[str, Any]]:
            return self.events

    siris = get_siris()
    frigate = Frigate()
    monkeypatch.setattr(siris, "_frigate_client", frigate)
    monkeypatch.setattr(door, "_armed", lambda s: False)
    spoken: list[tuple[str, dict[str, Any]]] = []
    real = siris.run_tool

    def run_tool(name: str, arguments: dict[str, Any], **kw: Any) -> Any:
        if name == "frigate_describe_event":
            return {"description": "A courier in a hi-vis vest holding a parcel."}
        if name in ("notify", "speak_in_current_room"):
            spoken.append((name, arguments))
            return {"sent": True}
        return real(name, arguments, **kw)

    monkeypatch.setattr(siris, "run_tool", run_tool)
    monkeypatch.setattr(siris.tools, "get", lambda n, _g=siris.tools.get: object() if n in ("frigate_describe_event", "notify", "speak_in_current_room") else _g(n))
    monkeypatch.setattr(siris.parcels, "active", lambda: [{"label": "new headphones", "carrier": "DHL", "status": "OutForDelivery"}])
    siris.state.delete(door.CURSOR_KEY)
    siris.state.delete(door.LAST_KEY)
    assert door.check(siris) == []                                 # the first look just sets the starting point
    frigate.events = [{"id": "e1", "camera": "front_door", "start_time": "9999-01-01T00:00:00"},
                      {"id": "e2", "camera": "backyard", "start_time": "9999-01-01T00:00:01"}]
    found = door.check(siris)
    assert [f["camera"] for f in found] == ["front_door"] and found[0]["parcel"] == "new headphones"
    assert "Probably your new headphones from DHL" in found[0]["message"] and {n for n, _ in spoken} == {"notify", "speak_in_current_room"}
    frigate.events.append({"id": "e3", "camera": "front_door", "start_time": "9999-01-01T00:00:05"})
    assert door.check(siris) == []                                 # 3-minute cooldown


# -- setup check --------------------------------------------------------------

def test_setup_check(client: TestClient, monkeypatch: Any) -> None:
    siris = get_siris()
    monkeypatch.setattr(siris.settings, "cloud_llm_when", "hard")
    monkeypatch.setattr(siris.settings, "hidden_tools", "no_such_tool")
    report = client.get("/siris/upkeep/setup").json()
    assert report["summary"].count("parts are set up") == 1
    assert any("hosted model" in p for p in report["problems"]) and any("no_such_tool" in p for p in report["problems"])
    assert all("api_key" not in json.dumps(f).lower() or "SIRISAI_" in json.dumps(f) for f in report["features"])
    assert siris.tools.get("setup_check").run()["problems"]


# -- can't-do suggestions ----------------------------------------------------

def test_cant_do_requests_become_suggestions(client: TestClient, monkeypatch: Any) -> None:
    from app.capabilities import review

    siris = get_siris()
    for text in ("add milk to the shopping list", "put eggs on my shopping list", "translate this into French"):
        siris.capability_gaps.report_gap(text, suggested_integration=None)
    groups = review.group([g for g in siris.capability_gaps.list_gaps(status="open") if "shopping" in g["requested_capability"] or "French" in g["requested_capability"]])
    assert [len(g) for g in groups][:2] == [2, 1]
    out = review.review(siris)
    shopping = next(o for o in out if "shopping" in o["request"])
    assert shopping["asked"] == 2 and shopping["kind"] in ("setting", "works_now", "build")
    item = next(i for i in siris.attention.list("open") if i["source"] == "capability_review" and "shopping" in i["title"])
    act(siris, item["id"], "dismiss_gaps")
    assert not [g for g in siris.capability_gaps.list_gaps(status="open") if "shopping list" in g["requested_capability"]]


# -- meals --------------------------------------------------------------------

class _Lists:
    def __init__(self) -> None:
        self.items = {"todo.pantry": ["chicken", "rice", "garlic"], "todo.shopping_list": ["milk"]}

    def list_states(self, domain: str | None = None) -> list[dict[str, Any]]:
        return [{"entity_id": "todo.pantry", "attributes": {"friendly_name": "Pantry"}},
                {"entity_id": "todo.shopping_list", "attributes": {"friendly_name": "Shopping List"}}]

    def get_todo_items(self, entity_id: str, status: str | None = "needs_action") -> list[dict[str, Any]]:
        return [{"summary": s} for s in self.items.get(entity_id, [])]

    def add_todo_item(self, entity_id: str, item: str) -> None:
        self.items[entity_id].append(item)


def test_meal_context_plan_and_shopping(client: TestClient, monkeypatch: Any) -> None:
    from app.household import meals

    siris = get_siris()
    lists = _Lists()
    monkeypatch.setattr(siris, "_home_assistant_client", lists)
    monkeypatch.setattr(siris.settings, "meal_preferences", "no mushrooms")
    found = meals.context(siris, have="eggs")
    assert found["have"] == ["chicken", "rice", "garlic", "eggs"] and found["already_on_shopping_list"] == ["milk"]
    assert found["preferences"] == "no mushrooms" and len(found["week"]) == 7
    today = datetime.now(UTC).strftime("%A")
    saved = meals.save(siris, [{"day": today, "meal": "Garlic chicken and rice", "ingredients": ["chicken", "rice", "garlic", "lemon", "milk"]},
                               {"day": "Someday", "meal": "Omelette", "ingredients": ["eggs", "lemon", "chives"]}])
    assert saved["added_to_shopping_list"] == ["lemon", "eggs", "chives"]   # not what's in, nor what's already listed
    assert meals.tonight(siris) == "Dinner tonight: Garlic chicken and rice."
    assert siris.tools.get("meal_plan").run()["plan"][0]["meal"] == "Garlic chicken and rice"
    siris.state.delete(meals.PLAN_KEY)
