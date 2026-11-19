"""7.27: learning from corrections, trimming tools, "me" per speaker, a hosted model,
number checks, the Plex -> Sonarr hand-off and watchlist, music, and the voice speed target."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.core import identity
from app.core.config import Settings
from app.core.siris import get_siris
from app.core.user_context import set_current_user_id, set_turn_role
from app.hub.attention import act
from app.llm.base import LLMResponse


# -- learning from corrections -----------------------------------------------

def test_a_fix_is_proposed_tried_and_applied(client: TestClient, monkeypatch: Any) -> None:
    from app.learning import fixes
    from app.learning.reflection import LEARNED_CASES_KEY

    siris = get_siris()
    tool = siris.tools.list()[0].name
    prompts = ["zork the quibble frobnicator", "please zork my frobnicator quibble"]
    siris.state.set(LEARNED_CASES_KEY, [{"prompt": p, "expect": [tool]} for p in prompts])
    siris.state.delete(fixes.FIXES_KEY)
    monkeypatch.setattr(siris.tool_selector, "max_tools", 12)   # the 9 core tools plus 3
    monkeypatch.setattr(siris.settings, "llm_provider", "none")
    try:
        raised = fixes.propose(siris)
        assert raised and raised[0]["tool"] == tool and raised[0]["offered"][1] == 2
        item = next(i for i in siris.attention.list("open") if i["key"] == f"tool_fixes:{tool}")
        act(siris, item["id"], "apply")
        assert fixes.approved(siris)[tool] == prompts
        spec = next(s for s in siris.available_tool_specs(prompts[0]) if s["function"]["name"] == tool)
        assert "zork the quibble frobnicator" in spec["function"]["description"]
        assert fixes.propose(siris) == []          # nothing left to learn
        assert client.delete(f"/siris/learning/fixes/{tool}").json()["forgotten"]
    finally:
        siris.state.delete(LEARNED_CASES_KEY)
        siris.state.delete(fixes.FIXES_KEY)


# -- trimming unused tools ---------------------------------------------------

def test_unused_tools_are_proposed_and_hidden(client: TestClient, monkeypatch: Any) -> None:
    from app.core import settings_overrides
    from app.upkeep import tool_usage

    siris = get_siris()
    now = datetime.now(UTC)
    monkeypatch.setattr(tool_usage, "audit_days", lambda s, n=None: 90.0)
    names = tool_usage.candidates(siris, now)
    assert names and "remember" not in names   # core tools are never offered for hiding
    item = tool_usage.propose(siris, now)
    assert item is not None and siris.attention._payload(item["id"])["tools"] == names
    monkeypatch.setattr(settings_overrides, "path", lambda: __import__("pathlib").Path(__import__("tempfile").mkdtemp()) / "s.json")
    try:
        act(siris, item["id"], "hide")
        assert set(names) <= siris.hidden_tool_names()
        offered = {s["function"]["name"] for s in siris.available_tool_specs()}
        assert not offered & set(names)
    finally:
        siris.settings.hidden_tools = ""


def test_no_proposal_before_there_is_enough_history(client: TestClient, monkeypatch: Any) -> None:
    from app.upkeep import tool_usage

    monkeypatch.setattr(tool_usage, "audit_days", lambda s, n=None: 5.0)
    assert tool_usage.candidates(get_siris()) == []


# -- "me" follows the speaker ------------------------------------------------

def test_me_is_whoever_is_speaking() -> None:
    settings = Settings(my_accounts="plex=Brad,brad6491", people_accounts="sam: plex=Sam; calendar=Family | jo: calendar=Jo", owner_user_ids="brad")
    try:
        assert identity.names(settings, "plex") == ["Brad", "brad6491"]          # the default user: you
        set_current_user_id("sam")
        assert identity.names(settings, "plex") == ["Sam"] and identity.names(settings, "calendar") == ["Family"]
        set_current_user_id("jo")
        assert identity.names(settings, "plex") == ["jo"]                        # not listed for Plex: by user id
        set_current_user_id("brad")
        assert identity.names(settings, "plex") == ["Brad", "brad6491"]          # your own voice
        set_current_user_id("sam")
        assert identity.names(Settings(my_accounts="plex=Brad"), "plex") == ["Brad"]   # nobody else listed: as before
    finally:
        set_current_user_id("default")


# -- the hosted model --------------------------------------------------------

def test_hosted_model_rules(client: TestClient, monkeypatch: Any) -> None:
    from app.llm import cloud

    siris = get_siris()
    monkeypatch.setattr(siris, "cloud_llm", object())
    for k, v in {"cloud_llm_when": "hard", "cloud_llm_base_url": "http://x/v1", "cloud_llm_model": "m", "cloud_llm_monthly_calls": 2}.items():
        monkeypatch.setattr(siris.settings, k, v)
    siris.state.delete(cloud._month_key())
    assert cloud.choose(siris, "plan a three day trip to Tasmania") is siris.cloud_llm
    assert cloud.choose(siris, "turn on the lights") is None                     # not hard
    assert cloud.choose(siris, "plan my training week around my sleep") is None   # private: stays local
    set_turn_role("guest")
    assert cloud.choose(siris, "plan a trip") is None
    set_turn_role("adult")
    cloud.count(siris)
    cloud.count(siris)
    assert cloud.choose(siris, "plan a trip") is None                            # the monthly cap
    monkeypatch.setattr(siris.settings, "cloud_llm_when", "never")
    siris.state.delete(cloud._month_key())
    assert cloud.choose(siris, "plan a trip") is None and cloud.fallback(siris, "hi") is None


def test_the_openai_compatible_client() -> None:
    from app.llm.openai_compat import OpenAICompatibleLLMClient, to_openai

    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        assert request.headers["authorization"] == "Bearer k"
        return httpx.Response(200, json={"choices": [{"message": {"content": None, "tool_calls": [
            {"id": "a", "type": "function", "function": {"name": "weather_current", "arguments": '{"city": "Perth"}'}}]}}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 3}})

    llm = OpenAICompatibleLLMClient("http://p/v1", "k", "m", transport=httpx.MockTransport(handler))
    history = [{"role": "user", "content": "weather?"},
               {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "x", "arguments": {"a": 1}}}]},
               {"role": "tool", "content": "{}"}]
    out = asyncio.run(llm.generate(history, tools=[{"type": "function", "function": {"name": "weather_current"}}]))
    assert out.tool_calls[0].name == "weather_current" and out.tool_calls[0].arguments == {"city": "Perth"} and out.prompt_tokens == 12
    sent = seen[0]["messages"]
    assert sent[1]["tool_calls"][0]["function"]["arguments"] == '{"a": 1}' and sent[2]["tool_call_id"] == sent[1]["tool_calls"][0]["id"]
    assert to_openai([{"role": "user", "content": None}]) == [{"role": "user", "content": ""}]


def test_a_failed_local_model_falls_back_to_the_hosted_one(client: TestClient, monkeypatch: Any) -> None:
    from app.cortex.orchestrator import Cortex
    from app.llm import cloud

    class Broken:
        async def generate(self, *a: Any, **k: Any) -> LLMResponse:
            raise ConnectionError("ollama down")

        async def stream_generate(self, *a: Any, **k: Any) -> Any:
            raise ConnectionError("ollama down")
            yield  # pragma: no cover

    class Hosted:
        async def generate(self, *a: Any, **k: Any) -> LLMResponse:
            return LLMResponse(text="Here's your answer, sir.")

        async def stream_generate(self, *a: Any, **k: Any) -> Any:
            from app.llm.base import LLMChunk

            yield LLMChunk(content="Here's your answer, sir.")
            yield LLMChunk(response=LLMResponse(text="Here's your answer, sir."))

    siris = get_siris()
    monkeypatch.setattr(siris, "llm", Broken())
    monkeypatch.setattr(siris, "cloud_llm", Hosted())
    for k, v in {"cloud_llm_when": "fallback", "cloud_llm_base_url": "http://x/v1", "cloud_llm_model": "m", "cloud_llm_monthly_calls": 5}.items():
        monkeypatch.setattr(siris.settings, k, v)
    siris.state.delete(cloud._month_key())
    conv = siris.conversation.create_conversation()
    result = asyncio.run(Cortex().process(siris, conv, "tell me a fun fact about otters", use_tools=False))
    assert result.response == "Here's your answer, sir." and result.model_role == "cloud"
    assert cloud.used_this_month(siris) == 1


# -- numbers -----------------------------------------------------------------

def test_unsupported_numbers() -> None:
    from app.cortex.numbers import unsupported

    data = [{"temperature_c": 14.2, "humidity": 0.61, "parcels": [{"id": 1}, {"id": 2}, {"id": 3}], "free_mb": 2048}]
    assert unsupported("It's 14 degrees with 61% humidity, and 2 GB free.", data) == []
    assert unsupported("You have 3 parcels on the way.", data) == []
    assert unsupported("It's 21 degrees outside.", data) == [21.0]
    assert unsupported("At 3:30pm on 2026-11-19 it was 14 degrees.", data) == []
    assert unsupported("Set it to 25 degrees.", data, prompt="set it to 25") == []


def test_a_wrong_number_gets_a_correction(client: TestClient, monkeypatch: Any) -> None:
    from app.cortex import numbers
    from app.cortex.orchestrator import CortexResult, ToolCallOutcome

    class Checker:
        def __init__(self, reply: str) -> None:
            self.reply = reply

        async def generate(self, *a: Any, **k: Any) -> LLMResponse:
            return LLMResponse(text=self.reply)

    siris = get_siris()
    monkeypatch.setattr(siris.settings, "check_numbers", True)
    outcome = ToolCallOutcome("weather_current", {}, {"temperature_c": 14.2})
    monkeypatch.setattr(siris, "llm", Checker("It's 14.2 °C outside."))
    wrong = CortexResult(conversation_id="c", response="It's 21 degrees, sir.", tool_call=outcome)
    asyncio.run(numbers.check(siris, wrong, "how warm is it?"))
    assert wrong.response.endswith("Correction: It's 14.2 °C outside.") and wrong.number_check["unsupported"] == [21.0]
    monkeypatch.setattr(siris, "llm", Checker("OK"))
    worked_out = CortexResult(conversation_id="c", response="That's 22 degrees warmer than yesterday.", tool_call=outcome)
    asyncio.run(numbers.check(siris, worked_out, "?"))
    assert "Correction" not in worked_out.response


# -- watching ----------------------------------------------------------------

def test_finished_seasons() -> None:
    from app.media.watching import finished_seasons

    rows = [{"show": "Slow Horses", "season": 4, "episode": 6}, {"show": "Slow Horses", "season": 4, "episode": 5},
            {"show": "The Bear", "season": 3, "episode": 4}, {"show": "Severance", "season": 2, "episode": 10}]
    series = [
        {"id": 1, "title": "Slow Horses", "seasons": [{"season": 4, "episodes": 6, "files": 6, "aired": True, "monitored": True},
                                                       {"season": 5, "episodes": 6, "files": 0, "aired": True, "monitored": False}]},
        {"id": 2, "title": "The Bear", "seasons": [{"season": 3, "episodes": 10, "files": 10, "aired": True, "monitored": True},
                                                    {"season": 4, "episodes": 10, "files": 0, "aired": True, "monitored": False}]},
        {"id": 3, "title": "Severance", "seasons": [{"season": 2, "episodes": 10, "files": 10, "aired": True, "monitored": True},
                                                     {"season": 3, "episodes": 0, "files": 0, "aired": False, "monitored": True}]},
    ]
    assert finished_seasons(rows, series) == [{"show": "Slow Horses", "series_id": 1, "season": 5, "monitored": False}]


def test_get_season_from_the_feed(client: TestClient, monkeypatch: Any) -> None:
    from app.media import watching
    from app.sonarr.client import SonarrClient

    calls: list[tuple[str, str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        calls.append((request.method, request.url.path, body))
        if request.method == "GET" and request.url.path == "/api/v3/series":
            return httpx.Response(200, json=[{"id": 1, "title": "Slow Horses", "seasons": [
                {"seasonNumber": 4, "monitored": True, "statistics": {"episodeFileCount": 6, "totalEpisodeCount": 6, "previousAiring": "x"}},
                {"seasonNumber": 5, "monitored": False, "statistics": {"episodeFileCount": 0, "totalEpisodeCount": 6, "previousAiring": "x"}}]}])
        if request.method == "GET":
            return httpx.Response(200, json={"id": 1, "monitored": False, "seasons": [{"seasonNumber": 5, "monitored": False}]})
        return httpx.Response(200, json={})

    siris = get_siris()
    monkeypatch.setattr(siris, "_sonarr_client", SonarrClient("http://s", "k", transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(watching, "_my_rows", lambda s, length=100: [{"show": "Slow Horses", "season": 4, "episode": 6}])
    found = watching.check(siris)
    assert found["next_seasons"][0]["season"] == 5
    item = next(i for i in siris.attention.list("open") if i["key"] == "watching:season:1:5")
    act(siris, item["id"], "get_season")
    put = next(c for c in calls if c[0] == "PUT")
    assert put[2]["monitored"] and put[2]["seasons"][0]["monitored"]
    assert calls[-1][2] == {"name": "SeasonSearch", "seriesId": 1, "seasonNumber": 5}


def test_watchlist_comes_first_and_is_crossed_off(client: TestClient, monkeypatch: Any) -> None:
    from app.media import watching
    from app.plugins.builtin.plex import PlexSuggestTool

    siris = get_siris()
    siris.state.delete("watchlist")
    watching.add(siris, "Dune", "Sam's pick")
    watching.add(siris, "Arrival")

    class Plex:
        def on_deck(self) -> list[dict[str, Any]]:
            return []

        def recently_added(self, limit: int = 50) -> list[dict[str, Any]]:
            return [{"title": "Other", "type": "movie", "watched": False, "genres": [], "show": None}]

        def search(self, q: str) -> list[dict[str, Any]]:
            return [{"title": "Dune", "type": "movie", "year": 2021}] if q == "Dune" else []

    picks = PlexSuggestTool(Plex(), lambda: [], lambda: watching.watchlist(siris)).run()["suggestions"]  # type: ignore[arg-type]
    assert picks[0]["title"] == "Dune" and picks[0]["why"] == "on your watchlist"
    monkeypatch.setattr(siris, "_plex_client", Plex())
    monkeypatch.setattr(siris, "_sonarr_client", None)
    monkeypatch.setattr(watching, "_my_rows", lambda s, length=100: [{"title": "Arrival"}])
    found = watching.check(siris)
    assert found["watched"] == ["Arrival"] and found["arrived"] == ["Dune"]
    assert [i["title"] for i in watching.watchlist(siris)] == ["Dune"]
    assert watching.check(siris)["arrived"] == []   # said once
    siris.state.delete("watchlist")


# -- music -------------------------------------------------------------------

class _HA:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str | None, dict[str, Any]]] = []
        self.states = [
            {"entity_id": "media_player.lounge", "state": "playing", "attributes": {"friendly_name": "Lounge Speaker", "media_title": "Dreams", "media_artist": "Fleetwood Mac"}},
            {"entity_id": "media_player.kitchen", "state": "idle", "attributes": {"friendly_name": "Kitchen Speaker"}},
        ]

    def list_states(self, domain: str | None = None) -> list[dict[str, Any]]:
        return self.states

    def call_service(self, domain: str, service: str, entity_id: str | None = None, **data: Any) -> Any:
        self.calls.append((domain, service, entity_id, data))
        return []


def test_music_tools(client: TestClient, monkeypatch: Any) -> None:
    from app.plugins.builtin import music

    siris = get_siris()
    ha = _HA()
    monkeypatch.setattr(siris, "_home_assistant_client", ha)
    monkeypatch.setattr(siris.satellites, "current_room", lambda: "kitchen")
    assert music.MusicPlayTool(siris).run(query="jazz")["on"] == "Kitchen Speaker"            # the room you're in
    assert ha.calls[-1] == ("music_assistant", "play_media", "media_player.kitchen", {"media_id": "jazz"})
    assert music.MusicPlayTool(siris).run(query="Rumours", room="lounge", media_type="album")["on"] == "Lounge Speaker"
    assert music.MusicNowPlayingTool(siris).run()[0] == {"where": "Lounge Speaker", "title": "Dreams", "artist": "Fleetwood Mac", "album": None}
    assert music.MusicPauseAllTool(siris).run() == {"paused": ["Lounge Speaker"]}
    assert music.MusicMoveTool(siris).run(to_room="kitchen") == {"moved": True, "from": "Lounge Speaker", "to": "Kitchen Speaker"}
    assert ha.calls[-1] == ("music_assistant", "transfer_queue", "media_player.kitchen", {"source_player": "media_player.lounge"})


def test_music_follows_you(client: TestClient, monkeypatch: Any) -> None:
    from app.plugins.builtin import music

    siris = get_siris()
    ha = _HA()
    monkeypatch.setattr(siris, "_home_assistant_client", ha)
    monkeypatch.setattr(siris.settings, "music_follow_me", True)
    room = {"now": "lounge"}
    monkeypatch.setattr(siris.satellites, "current_room", lambda: room["now"])
    siris.state.delete("music:follow_room")
    assert music.follow(siris) is None          # first sighting: just remembered
    room["now"] = "kitchen"
    assert music.follow(siris) == {"moved": True, "from": "Lounge Speaker", "to": "Kitchen Speaker"}
    siris.state.delete("music:follow_room")


# -- the voice speed target --------------------------------------------------

def test_time_to_first_audio_is_measured_and_reported(client: TestClient, monkeypatch: Any) -> None:
    from app.upkeep import dashboard, reliability

    siris = get_siris()
    lines = client.post("/siris/voice/converse", data={"text": "what time is it?", "synthesize": "false"}).text
    assert '"type": "final"' in lines
    now = datetime.now(UTC)
    siris.metrics.record_samples({"voice_first_audio_ms": 600})
    for v in (700, 900, 1500, 2000):
        siris.metrics.record_sample("voice_first_audio_ms", v)
    voice = reliability.voice_speed(siris, now - timedelta(minutes=5), now + timedelta(minutes=1))
    assert voice["turns"] >= 5 and voice["target_ms"] == 1000 and voice["within_target_percent"] is not None
    assert "voice" in dashboard.build(siris)


def test_new_settings_are_editable() -> None:
    from app.core.settings_overrides import EDITABLE

    for name in ("hidden_tools", "people_accounts", "owner_user_ids", "cloud_llm_when", "cloud_llm_monthly_calls",
                 "check_numbers", "music_follow_me", "voice_target_ms"):
        assert name in EDITABLE, name
    assert "cloud_llm_api_key" not in EDITABLE   # secrets stay in .env
