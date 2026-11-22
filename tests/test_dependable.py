"""7.17: dependable answers, the voice, "me" everywhere, Plex suggestions and upkeep."""

import asyncio
from pathlib import Path
from typing import Any

import httpx
from fastapi.testclient import TestClient

from app.core import identity
from app.core.config import Settings
from app.core.siris import get_siris
from app.cortex import persona
from app.evals import tool_choice
from app.learning.reflection import LEARNED_CASES_KEY
from app.llm.base import LLMResponse
from app.plex.client import PlexClient
from app.plugins.builtin.plex import PlexNowPlayingTool, PlexSuggestTool
from app.voice.sentences import is_stop


# -- "that was wrong" -------------------------------------------------------

def test_a_thumbs_down_becomes_a_case_to_avoid_or_expect(client: TestClient) -> None:
    siris = get_siris()
    conv = siris.conversation.create_conversation()
    siris.conversation.add_message(conv, "user", "what have I watched lately?")
    siris.conversation.add_message(conv, "tool", "[]", tool_name="plex_now_playing")
    siris.conversation.add_message(conv, "assistant", "Nothing is playing, sir.")
    siris.state.delete(LEARNED_CASES_KEY)

    entry = client.post("/siris/learning/feedback", json={"conversation_id": conv}).json()
    assert entry["request"] == "what have I watched lately?" and entry["reply"] == "Nothing is playing, sir."
    assert entry["case"]["avoid"] == ["plex_now_playing"]
    assert siris.outcomes.recent(signal="corrected")[0]["subject"] == "plex_now_playing"

    entry = client.post("/siris/learning/feedback", json={"conversation_id": conv, "right_tool": "remember"}).json()
    assert entry["case"]["expect"] == ["remember"]
    cases = [c for c in siris.state.get(LEARNED_CASES_KEY) if c["prompt"] == "what have I watched lately?"]
    assert len(cases) == 1 and cases[0]["expect"] == ["remember"]   # replaced, not duplicated
    assert client.get("/siris/learning/feedback").json()[0]["right_tool"] == "remember"
    assert client.post("/siris/learning/feedback", json={"conversation_id": conv, "right_tool": "nope"}).status_code == 422
    assert client.post("/siris/learning/feedback", json={"conversation_id": "none-such"}).status_code == 404
    siris.state.delete(LEARNED_CASES_KEY)


def test_avoid_cases_pass_unless_the_same_tool_is_chosen() -> None:
    assert tool_choice._ok({"avoid": ["plex_now_playing"]}, "tautulli_recent_history")
    assert not tool_choice._ok({"avoid": ["plex_now_playing"]}, "plex_now_playing")
    assert tool_choice._ok({"expect": ["a"]}, "a") and not tool_choice._ok({"expect": ["a"]}, None)


def test_a_call_written_as_text_counts_as_the_choice(client: TestClient) -> None:
    class Writer:
        async def generate(self, messages: Any, tools: Any = None, **kw: Any) -> LLMResponse:
            return LLMResponse(text='{"name": "tautulli_recent_history", "parameters": {"user": "me"}}')

    assert asyncio.run(tool_choice._choose(get_siris(), "what have I watched?", Writer())) == "tautulli_recent_history"


def test_the_evals_run_nightly_and_when_the_model_changes(client: TestClient) -> None:
    assert Settings().eval_interval_days == 1.0
    siris = get_siris()
    saved = siris.state.get(tool_choice.HISTORY_KEY)
    try:
        siris.state.set(tool_choice.HISTORY_KEY, [{"model": "ollama:old"}])
        provider, model = siris.settings.llm_provider, siris.settings.ollama_model
        siris.settings.llm_provider, siris.settings.ollama_model = "ollama", "qwen3:8b"
        assert tool_choice.due_for_new_model(siris)
        siris.state.set(tool_choice.HISTORY_KEY, [{"model": "ollama:qwen3:8b"}])
        assert not tool_choice.due_for_new_model(siris)
        siris.settings.llm_provider, siris.settings.ollama_model = provider, model
    finally:
        siris.state.set(tool_choice.HISTORY_KEY, saved or [])


def test_new_cases_are_checked_in() -> None:
    prompts = {c["prompt"] for c in tool_choice.load_cases()}
    assert {"what have I watched recently?", "what should I watch tonight?"} <= prompts


# -- voice ------------------------------------------------------------------

def test_stop_words() -> None:
    for said in ("Stop", "stop talking.", "never mind", "Siris, that's all", "OK stop", "cancel"):
        assert is_stop(said), said
    for said in ("stop the music in the lounge", "cancel my 3pm meeting", "what's on tonight?"):
        assert not is_stop(said), said


def test_stop_ends_a_spoken_turn_without_the_model(client: TestClient) -> None:
    lines = client.post("/siris/voice/converse", data={"text": "never mind", "synthesize": "false"}).text.strip().splitlines()
    assert '"stopped": true' in lines[-1]


def test_fillers_follow_the_personality() -> None:
    jarvis = Settings(persona_style="jarvis")
    assert persona.filler(jarvis) == "One moment, sir." and persona.filler(jarvis, 1) != persona.filler(jarvis)
    assert persona.filler(Settings(persona_style="plain")) == "One moment."


def test_a_slow_tool_gets_a_filler_line(client: TestClient, monkeypatch: Any) -> None:
    from app.voice import turn as turn_module

    class SlowCortex:
        def __init__(self, on_event: Any) -> None:
            self.on_event = on_event

        async def process(self, siris: Any, conv: str, text: str, use_planner: bool = False) -> Any:
            from app.cortex.orchestrator import CortexResult

            await self.on_event({"type": "tool_start", "name": "slow"})
            await asyncio.sleep(0.25)
            await self.on_event({"type": "tool_end", "name": "slow", "status": "ok"})
            result = CortexResult(conversation_id=conv, response="Done, sir.")
            await self.on_event({"type": "final", "result": result})
            return result

    class Speech:
        async def synthesize(self, text: str) -> bytes:
            return b"mp3"

    siris = get_siris()
    monkeypatch.setattr(turn_module, "Cortex", SlowCortex)
    monkeypatch.setattr(siris, "tts", Speech())
    monkeypatch.setattr(siris.settings, "voice_filler_seconds", 0.05)
    monkeypatch.setattr(siris.settings, "persona_style", "jarvis")

    async def collect() -> list[dict[str, Any]]:
        return [e async for e in turn_module.VoiceTurn(siris, "conv-filler", "check the slow thing").run()]

    events = asyncio.run(collect())
    said = [e["text"] for e in events if e["type"] == "sentence"]
    assert said[0] == "One moment, sir." and said[-1] == "Done, sir."
    assert sum(1 for e in events if e.get("filler")) <= 2


def test_room_announcements_use_the_same_voice_and_pace() -> None:
    from app.plugins.builtin.satellites import SpeakInCurrentRoomTool

    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        sent.append(json.loads(request.content))
        return httpx.Response(200, content=b"mp3")

    settings = Settings(persona_style="jarvis", tts_voice="bm_lewis", tts_effect="")
    tool = SpeakInCurrentRoomTool(None, "http://tts", "kokoro", "af_heart", transport=httpx.MockTransport(handler), settings=settings)  # type: ignore[arg-type]
    tool._speech("Dinner's ready.")
    assert sent[-1]["voice"] == "bm_lewis" and sent[-1]["speed"] == 1.2


# -- "me" everywhere -------------------------------------------------------

def test_my_accounts_map() -> None:
    settings = Settings(my_accounts="plex=Brad, brad_second; calendar=Personal", plex_username="ignored")
    assert identity.names(settings, "plex") == ["Brad", "brad_second"]
    assert identity.names(settings, "calendar") == ["Personal"]
    assert identity.names(Settings(plex_username="solo"), "plex") == ["solo"]   # the older setting still works
    assert identity.is_me("Me") and not identity.is_me("sam")


def _tautulli(seen: list[dict[str, str]]) -> Any:
    from app.tautulli.client import TautulliClient

    users = [{"user_id": 1, "username": "owner", "friendly_name": "Brad", "is_admin": 1},
             {"user_id": 3, "username": "flatmate", "friendly_name": "Sam", "is_admin": 0}]

    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        seen.append(params)
        if params["cmd"] == "get_users":
            return httpx.Response(200, json={"response": {"data": users}})
        if params["cmd"] == "get_activity":
            sessions = [{"session_id": "a", "full_title": "Dune", "user": "Brad", "user_id": 1},
                        {"session_id": "b", "full_title": "Love Island", "user": "Sam", "user_id": 3}]
            return httpx.Response(200, json={"response": {"data": {"sessions": sessions}}})
        return httpx.Response(200, json={"response": {"data": [{"stat_id": "top_movies", "stat_title": "Most Watched Movies", "rows": []}]}})

    return TautulliClient(base_url="http://t", api_key="k", transport=httpx.MockTransport(handler))


def test_what_am_i_watching_and_my_stats() -> None:
    from app.plugins.builtin.tautulli import TautulliCurrentActivityTool, TautulliHomeStatsTool

    seen: list[dict[str, str]] = []
    client = _tautulli(seen)
    assert [s["title"] for s in TautulliCurrentActivityTool(client).run(user="me")] == ["Dune"]
    assert len(TautulliCurrentActivityTool(client).run()) == 2
    mine = TautulliHomeStatsTool(client, lambda: "Sam").run(user="me")
    assert mine["user"] == "Sam" and seen[-1]["user_id"] == "3"
    TautulliHomeStatsTool(client).run()
    assert "user_id" not in seen[-1]


def test_plex_now_playing_for_me() -> None:
    class Plex:
        def now_playing(self) -> list[dict[str, Any]]:
            return [{"title": "Dune", "user": "Brad", "owner": True}, {"title": "Love Island", "user": "Sam", "owner": False}]

    assert [p["title"] for p in PlexNowPlayingTool(Plex(), lambda: []).run(user="me")] == ["Dune"]   # type: ignore[arg-type]
    assert [p["title"] for p in PlexNowPlayingTool(Plex(), lambda: ["sam"]).run(user="me")] == ["Love Island"]  # type: ignore[arg-type]


# -- what to watch ----------------------------------------------------------

def _plex() -> PlexClient:
    deck = [{"type": "episode", "grandparentTitle": "The Bear", "title": "Fishes", "parentIndex": 2, "index": 6}]
    added = [
        {"type": "movie", "title": "Seen Thriller", "viewCount": 1, "Genre": [{"tag": "Thriller"}], "addedAt": 10},
        {"type": "movie", "title": "Romcom", "Genre": [{"tag": "Romance"}], "addedAt": 30, "audienceRating": 9},
        {"type": "movie", "title": "New Thriller", "Genre": [{"tag": "Thriller"}], "addedAt": 20, "audienceRating": 6},
        {"type": "episode", "grandparentTitle": "Slow Horses", "title": "Ep", "parentIndex": 4, "index": 1, "addedAt": 40},
        {"type": "episode", "grandparentTitle": "Love Island", "title": "Ep", "parentIndex": 1, "index": 9, "addedAt": 50},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        rows = deck if request.url.path == "/library/onDeck" else added
        return httpx.Response(200, json={"MediaContainer": {"Metadata": rows}})

    return PlexClient("http://p", "t", transport=httpx.MockTransport(handler))


def test_suggestions_start_with_what_you_watch() -> None:
    tool = PlexSuggestTool(_plex(), lambda: ["Slow Horses - The Last Dance", "Dune"])
    picks = tool.run()["suggestions"]
    assert [p["title"] for p in picks] == ["The Bear: S02E06 Fishes", "Slow Horses: S04E01 Ep", "New Thriller", "Romcom"]
    assert picks[0]["why"] == "you're part-way through it" and picks[2]["why"] == "new, and you like thriller"
    assert "Love Island" not in str(picks)   # a show you don't watch
    assert [p["title"] for p in tool.run(kind="movie", count=1)["suggestions"]] == ["New Thriller"]


# -- upkeep -----------------------------------------------------------------

def test_version_guard_and_rebase(tmp_path: Path, monkeypatch: Any) -> None:
    import importlib.util

    spec = importlib.util.spec_from_file_location("bump", Path(__file__).resolve().parents[1] / "scripts" / "bump_version.py")
    bump = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(bump)  # type: ignore[union-attr]
    version, log, readme = tmp_path / "v.py", tmp_path / "CHANGELOG.md", tmp_path / "README.md"
    version.write_text('__version__ = "4.12.0"\n')
    log.write_text("# Changelog\n\n## 4.12.0 — 2026-11-19\n\n- Mine\n\n## 4.11.3 — 2026-11-18\n\n- Before\n")
    readme.write_text("1. **Done** -- Old (4.11.3).\n2. **Done** -- Mine (4.12.0). Detail.\n")
    monkeypatch.setattr(bump, "VERSION_FILE", version)
    monkeypatch.setattr(bump, "CHANGELOG", log)
    assert bump.check("4.11.4") == "4.12.0 follows main's 4.11.4"
    assert bump.check("7.61").startswith("4.12.0 is the first Section.Feature.Fix version")
    try:
        bump.check("4.12.0")
        raise AssertionError("should fail")
    except SystemExit as exc:
        assert "rebase 4.12.0" in str(exc)
    assert bump.rebase("4.12.1", readme) == "4.13.0"   # a feature stays a feature
    assert bump.current() == (4, 13, 0) and "## 4.13.0 — " in log.read_text() and "## 4.11.3 — " in log.read_text()
    assert "Mine (4.13.0)" in readme.read_text() and "Old (4.11.3)" in readme.read_text()
    assert bump.step((4, 9, 3), "feature") == (4, 10, 0) and bump.step((4, 9, 3), "section") == (5, 0, 0)
    assert bump.step((4, 9, 9), "small") == (4, 9, 10)  # never rolls over


def test_tool_usage_report(client: TestClient) -> None:
    siris = get_siris()
    name = siris.tools.list()[0].name
    siris.audit.record(name, {}, result={"ok": True})
    report = client.get("/siris/upkeep/tool-usage?days=30").json()
    assert name not in report["unused"] and any(r["tool"] == name for r in report["most_used"])
    assert report["registered"] >= report["used"] >= 1
