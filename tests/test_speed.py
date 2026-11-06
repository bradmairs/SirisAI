"""Speed work (4.6): instant home commands, per-turn timings, the prompt
order Ollama can cache, keep-alive and warm-up, the scheduler's separate
job loops, and the embedding and status caches."""

import asyncio
import json
import time
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from app.core import scheduler
from app.core.config import Settings
from app.core.siris import SirisAI
from app.cortex import quick, timings
from app.cortex.orchestrator import Cortex
from app.llm.base import LLMClient, LLMResponse
from app.llm.embeddings import OllamaEmbeddingClient
from app.llm.ollama import OllamaClient
from app.plugins.builtin.home_assistant import HomeAssistantCallServiceTool, HomeAssistantResolveEntityTool


class FakeHA:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Any, dict]] = []
        self.states = [
            {"entity_id": "light.kitchen", "state": "on", "attributes": {"friendly_name": "Kitchen Light"}},
            {"entity_id": "light.lounge_lamp", "state": "off", "attributes": {"friendly_name": "Lounge Lamp"}},
            {"entity_id": "light.hall", "state": "off", "attributes": {"friendly_name": "Hall"}},
            {"entity_id": "switch.hall_fan", "state": "off", "attributes": {"friendly_name": "Hall Fan"}},
            {"entity_id": "lock.front_door", "state": "locked", "attributes": {"friendly_name": "Front Door"}},
        ]

    def list_states(self, domain: str | None = None) -> list[dict]:
        return [s for s in self.states if domain is None or s["entity_id"].startswith(domain + ".")]

    def get_state_or_none(self, entity_id: str) -> dict | None:
        return next((s for s in self.states if s["entity_id"] == entity_id), None)

    def call_service(self, domain: str, service: str, entity_id: Any = None, **data: object) -> object:
        self.calls.append((domain, service, entity_id, data))
        return []


class NoModel(LLMClient):
    def __init__(self) -> None:
        self.calls = 0

    async def generate(self, messages, *, tools=None, **kwargs) -> LLMResponse:  # type: ignore[no-untyped-def]
        self.calls += 1
        return LLMResponse(text="from the model")


def _home(no_confirm: str = "light,switch,fan", **settings: Any) -> tuple[SirisAI, FakeHA, NoModel]:
    siris = SirisAI(settings=Settings(**settings))
    ha = FakeHA()
    siris.tools.register(HomeAssistantResolveEntityTool(ha))  # type: ignore[arg-type]
    siris.tools.register(HomeAssistantCallServiceTool(ha, siris.events, no_confirm.split(",")))  # type: ignore[arg-type]
    llm = NoModel()
    siris.llm = llm
    return siris, ha, llm


@pytest.mark.parametrize(
    ("text", "name", "service", "data"),
    [
        ("Turn off the kitchen lights", "kitchen lights", "turn_off", {}),
        ("switch the hall fan on please", "hall fan", "turn_on", {}),
        ("Siris, turn on the lounge lamp.", "lounge lamp", "turn_on", {}),
        ("dim the lounge lamp to 30%", "lounge lamp", "turn_on", {"brightness_pct": 30}),
        ("set the kitchen light to 0 percent", "kitchen light", "turn_off", {}),
    ],
)
def test_parses_the_plain_shapes(text: str, name: str, service: str, data: dict) -> None:
    command = quick.parse(text)
    assert command is not None
    assert (command.name, command.service, command.data) == (name, service, data)


@pytest.mark.parametrize(
    "text",
    [
        "turn off all the lights",
        "turn off the kitchen and hall lights",
        "turn on the kitchen light at 7",
        "turn off the heater when I leave",
        "what's the kitchen light doing?",
        "set the lounge lamp to 150%",
    ],
)
def test_leaves_anything_else_to_the_model(text: str) -> None:
    assert quick.parse(text) is None


def test_a_quick_command_runs_without_the_model() -> None:
    siris, ha, llm = _home()
    conversation = siris.conversation.create_conversation()
    result = asyncio.run(Cortex().process(siris, conversation, "turn off the kitchen lights"))
    assert ha.calls == [("light", "turn_off", "light.kitchen", {})]
    assert llm.calls == 0
    assert result.response == "Done, the Kitchen Light is off."
    assert result.tool_call is not None and result.tool_call.name == "home_assistant_call_service"
    assert siris.conversation.get_messages(conversation)[-1]["content"] == "Done, the Kitchen Light is off."
    assert siris.audit.list_recent(limit=1)[0]["tool_name"] == "home_assistant_call_service"
    assert result.timings["total"] >= 0 and timings.summary(siris)["recent"][0]["path"] == "quick"


def test_brightness_goes_to_lights_only() -> None:
    siris, ha, _ = _home()
    asyncio.run(Cortex().process(siris, siris.conversation.create_conversation(), "dim the lounge lamp to 40%"))
    assert ha.calls == [("light", "turn_on", "light.lounge_lamp", {"brightness_pct": 40})]


def test_two_matches_or_a_confirmation_go_to_the_model() -> None:
    siris, ha, llm = _home()
    # "hall" matches the hall light and the hall fan switch.
    asyncio.run(Cortex().process(siris, siris.conversation.create_conversation(), "turn on the hall", use_planner=False))
    assert ha.calls == [] and llm.calls == 1

    siris, ha, llm = _home(no_confirm="")  # every call needs confirming
    asyncio.run(Cortex().process(siris, siris.conversation.create_conversation(), "turn off the kitchen light", use_planner=False))
    assert ha.calls == [] and llm.calls == 1


def test_locks_are_never_quick() -> None:
    siris, ha, llm = _home(no_confirm="*")
    asyncio.run(Cortex().process(siris, siris.conversation.create_conversation(), "turn off the front door", use_planner=False))
    assert ha.calls == [] and llm.calls == 1


def test_can_be_turned_off() -> None:
    siris, ha, llm = _home(quick_commands=False)
    asyncio.run(Cortex().process(siris, siris.conversation.create_conversation(), "turn off the kitchen light", use_planner=False))
    assert ha.calls == [] and llm.calls == 1


class Recording(LLMClient):
    def __init__(self) -> None:
        self.sent: list[list[dict]] = []

    async def generate(self, messages, *, tools=None, **kwargs) -> LLMResponse:  # type: ignore[no-untyped-def]
        self.sent.append(messages)
        return LLMResponse(text="ok")


def test_the_changing_context_sits_just_before_the_newest_message() -> None:
    siris = SirisAI(settings=Settings(persona="You are Siris."))
    llm = Recording()
    siris.llm = llm
    conversation = siris.conversation.create_conversation()
    siris.conversation.add_message(conversation, role="user", content="first")
    siris.conversation.add_message(conversation, role="assistant", content="reply")
    asyncio.run(Cortex().process(siris, conversation, "second", use_tools=False))
    roles = [(m["role"], (m["content"] or "")[:7]) for m in llm.sent[0]]
    assert roles == [("system", "You are"), ("user", "first"), ("assistant", "reply"), ("system", "Context"), ("user", "second")]


def test_timings_are_recorded_per_stage() -> None:
    siris = SirisAI(settings=Settings())
    siris.llm = Recording()
    result = asyncio.run(Cortex().process(siris, siris.conversation.create_conversation(), "hello there", use_planner=False))
    assert {"context", "tools", "model", "total"} <= set(result.timings)
    summary = timings.summary(siris)
    assert summary["stages"]["total"]["p50_ms"] is not None and summary["stages"]["total"]["count"] >= 1


def test_ollama_keeps_the_model_loaded_and_can_warm_it() -> None:
    seen: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, json.loads(request.content)))
        if request.url.path == "/api/chat":
            return httpx.Response(200, json={"message": {"content": "hi"}})
        return httpx.Response(200, json={"done": True})

    client = OllamaClient("http://ollama", "qwen", transport=httpx.MockTransport(handler), keep_alive="30m")  # type: ignore[arg-type]

    async def go() -> None:
        await client.generate([{"role": "user", "content": "hi"}])
        await client.warm()
        await client.aclose()

    asyncio.run(go())
    assert seen[0][1]["keep_alive"] == "30m"
    assert seen[1] == ("/api/generate", {"model": "qwen", "keep_alive": "30m"})

    plain = OllamaClient("http://ollama", "qwen", transport=httpx.MockTransport(handler))  # type: ignore[arg-type]
    asyncio.run(plain.generate([{"role": "user", "content": "hi"}]))
    assert "keep_alive" not in seen[-1][1]


def test_warm_hours() -> None:
    siris = SirisAI(settings=Settings(ollama_warm_hours="6-23", timezone="UTC"))
    siris.llm = OllamaClient("http://ollama", "qwen")
    assert scheduler.should_warm(siris, datetime(2026, 11, 6, 7, tzinfo=UTC))
    assert not scheduler.should_warm(siris, datetime(2026, 11, 6, 3, tzinfo=UTC))
    siris.settings.ollama_warm_hours = "22-6"
    assert scheduler.should_warm(siris, datetime(2026, 11, 6, 3, tzinfo=UTC))
    siris.settings.ollama_warm_hours = ""
    assert not scheduler.should_warm(siris, datetime(2026, 11, 6, 7, tzinfo=UTC))


def test_a_stuck_job_does_not_hold_up_reminders(monkeypatch: pytest.MonkeyPatch) -> None:
    siris = SirisAI(settings=Settings(server_watch_minutes=0.001, proactive_check_interval_minutes=0, presence_check_interval_minutes=0))
    reminders: list[int] = []
    servers: list[int] = []

    def stuck(_s: SirisAI) -> None:
        servers.append(1)
        time.sleep(0.5)

    monkeypatch.setattr(scheduler, "run_server_watch", stuck)
    monkeypatch.setattr(scheduler, "deliver_due_reminders", lambda s, now=None: reminders.append(1) or 0)
    monkeypatch.setattr(scheduler, "maybe_run_daily_briefing", lambda s, now=None: False)
    monkeypatch.setattr(scheduler, "run_daily_jobs", lambda s, now=None: {})

    async def go() -> None:
        task = asyncio.create_task(scheduler.run(siris, interval_seconds=0.01))
        await asyncio.sleep(0.3)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(go())
    assert len(reminders) >= 5  # kept ticking while the server check was stuck
    assert servers == [1]  # and the stuck check wasn't started again on top of itself
    status = {j["name"]: j for j in scheduler.job_status()}
    assert status["servers"]["running"] is True


def test_the_same_text_is_embedded_once() -> None:
    posts: list[Any] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        posts.append(body["input"])
        n = len(body["input"]) if isinstance(body["input"], list) else 1
        return httpx.Response(200, json={"embeddings": [[0.1, 0.2]] * n})

    client = OllamaEmbeddingClient("http://ollama", "nomic", transport=httpx.MockTransport(handler))
    assert client.embed("lights") == [0.1, 0.2]
    assert client.embed_many(["lights"]) == [[0.1, 0.2]]
    assert client.embed("lights") == [0.1, 0.2]
    assert posts == ["lights"]


def test_scheduler_and_timings_routes(client) -> None:  # type: ignore[no-untyped-def]
    assert client.get("/siris/timings").status_code == 200
    r = client.get("/siris/scheduler/jobs")
    assert r.status_code == 200 and isinstance(r.json(), list)
