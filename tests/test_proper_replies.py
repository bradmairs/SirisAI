"""6.7: tool calls written as text never reach the chat or the voice, a
follow-up may ask for another tool, the Jarvis style and pace, and "my" Plex
history."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.siris import SirisAI
from app.cortex import persona
from app.cortex.orchestrator import Cortex
from app.llm.base import LLMChunk, LLMClient, LLMResponse, ToolCall
from app.llm.streaming import generate_with_events, written_call
from app.plugins.base import Tool, ToolPermission


@pytest.mark.parametrize(
    ("text", "before", "name", "args"),
    [
        ('{"name": "brain_get_note", "parameters": {"note": "SirisAI – History"}}', "", "brain_get_note", {"note": "SirisAI – History"}),
        ('Still not right, sir. Let me try again. {"name": "tautulli_recent_history", "parameters": {"user": "me"}}',
         "Still not right, sir. Let me try again.", "tautulli_recent_history", {"user": "me"}),
        ('```json\n{"name": "weather_current", "arguments": {}}\n```', "", "weather_current", {}),
    ],
)
def test_a_written_tool_call_is_found(text: str, before: str, name: str, args: dict) -> None:
    found = written_call(text)
    assert found is not None and found[0] == before and found[1] == ToolCall(name=name, arguments=args)


@pytest.mark.parametrize(
    "text",
    [
        'Here is an example: {"name": "x_tool", "parameters": {}} and that is how it looks.',  # more answer after it
        'The settings are {"theme": "dark"}.',
        "Nothing like JSON here.",
        '{"name": "Brad", "parameters": {}}',  # not a tool-shaped name
    ],
)
def test_ordinary_text_is_left_alone(text: str) -> None:
    assert written_call(text) is None


class Streamer(LLMClient):
    """Streams the given pieces, then a final response with the same text and no calls."""

    def __init__(self, *replies: list[str]) -> None:
        self.replies = list(replies)
        self.calls: list[list[dict[str, Any]]] = []

    async def generate(self, messages, *, tools=None, **kwargs) -> LLMResponse:  # type: ignore[no-untyped-def]
        self.calls.append(messages)
        return LLMResponse(text="".join(self.replies.pop(0)))

    async def stream_generate(self, messages, *, tools=None, **kwargs):  # type: ignore[no-untyped-def]
        self.calls.append(messages)
        pieces = self.replies.pop(0)
        for piece in pieces:
            yield LLMChunk(content=piece)
        yield LLMChunk(response=LLMResponse(text="".join(pieces)))


def test_the_json_is_never_streamed_to_the_screen() -> None:
    shown: list[str] = []

    async def on_event(event: dict[str, Any]) -> None:
        if event["type"] == "content":
            shown.append(event["delta"])

    llm = Streamer(["Still not showing yours, sir. ", "Let me try again. {\"name\": ", "\"tautulli_recent_history\", ",
                    "\"parameters\": {\"user\": \"me\"}}"])
    out = asyncio.run(generate_with_events(llm, [], on_event))
    assert "".join(shown).rstrip() == "Still not showing yours, sir. Let me try again."
    assert out.tool_calls == [ToolCall(name="tautulli_recent_history", arguments={"user": "me"})]

    shown.clear()
    plain = Streamer(["The config is ", "{\"theme\": \"dark\"}", ", sir."])
    out = asyncio.run(generate_with_events(plain, [], on_event))
    assert "".join(shown) == "The config is {\"theme\": \"dark\"}, sir." and out.tool_calls == []


class Note(Tool):
    name = "brain_get_note"
    description = "a note"
    permission = ToolPermission.READ
    parameters = {"type": "object", "properties": {"note": {"type": "string"}}, "required": []}
    ran: list[dict[str, Any]] = []

    def run(self, **kwargs: Any) -> Any:
        Note.ran.append(kwargs)
        return {"note": kwargs.get("note"), "body": "Watched: The Bear"}


class History(Tool):
    name = "plex_history_test"
    description = "history"
    permission = ToolPermission.READ
    parameters = {"type": "object", "properties": {}, "required": []}

    def run(self, **kwargs: Any) -> Any:
        return [{"title": "The Bear"}]


def test_a_tool_written_in_the_answer_is_run_and_answered_from(client: TestClient) -> None:
    siris = SirisAI(settings=Settings(persona_style="plain"))
    siris.tools.register(Note())
    siris.tools.register(History())
    Note.ran = []

    class Script(LLMClient):
        def __init__(self) -> None:
            self.n = 0

        async def generate(self, messages, *, tools=None, **kwargs) -> LLMResponse:  # type: ignore[no-untyped-def]
            self.n += 1
            if self.n == 1:
                return LLMResponse(tool_calls=[ToolCall(name="plex_history_test", arguments={})])
            if self.n == 2:  # the answer asks for another tool, as text
                return LLMResponse(text='{"name": "brain_get_note", "parameters": {"note": "SirisAI"}}')
            return LLMResponse(text="You last watched The Bear, sir.")

    siris.llm = Script()
    result = asyncio.run(Cortex().process(siris, siris.conversation.create_conversation(), "what did I watch", use_planner=False))
    assert Note.ran == [{"note": "SirisAI"}] and result.response == "You last watched The Bear, sir."
    assert "{" not in (result.response or "")


def test_the_jarvis_style_and_pace() -> None:
    jarvis = persona.system_text(Settings(persona_style="jarvis", persona="Call me Brad."))
    assert jarvis is not None and jarvis.startswith("You are SirisAI") and jarvis.endswith("Call me Brad.")
    assert "Never show tool names, JSON, code" in jarvis
    assert persona.system_text(Settings(persona_style="plain", persona="")) is None
    assert persona.tts_speed(Settings(persona_style="jarvis")) == 1.2
    assert persona.tts_speed(Settings(persona_style="jarvis", tts_speed=1.05)) == 1.05
    assert persona.tts_speed(Settings(persona_style="plain")) == 1.0


def test_the_voice_changes_live_from_settings(client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    from app.core import settings_overrides
    from app.voice.openai_compatible import OpenAICompatibleTTSClient

    monkeypatch.setattr(settings_overrides, "path", lambda: tmp_path / "overrides.json")
    siris = SirisAI(settings=Settings(tts_provider="openai_compatible", persona_style="jarvis"))
    siris.tts = OpenAICompatibleTTSClient("http://tts", "kokoro", "af_heart", speed=1.2)
    settings_overrides.update(siris, {"tts_speed": 1.3, "tts_voice": "bm_lewis"})
    assert (siris.tts.speed, siris.tts.voice) == (1.3, "bm_lewis")
    settings_overrides.update(siris, {"tts_speed": None})
    assert siris.tts.speed == 1.2  # back to the personality's pace


def test_my_plex_history_is_only_mine() -> None:
    from app.plugins.builtin.tautulli import TautulliRecentHistoryTool
    from app.tautulli.client import TautulliClient

    seen: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        seen.append(params)
        if params["cmd"] == "get_users":
            return httpx.Response(200, json={"response": {"data": [{"username": "guest1", "is_admin": 0}, {"username": "bradm", "is_admin": 1}]}})
        return httpx.Response(200, json={"response": {"data": {"data": [{"full_title": "The Bear", "user": params.get("user", "x"), "date": 1}]}}})

    client = TautulliClient(base_url="http://t", api_key="k", transport=httpx.MockTransport(handler))
    mine = TautulliRecentHistoryTool(client).run(user="me")
    assert mine["user"] == "bradm" and seen[-1]["user"] == "bradm"
    named = TautulliRecentHistoryTool(client, my_username="brad_plex").run(user="mine", length=5)
    assert named["user"] == "brad_plex" and seen[-1]["user"] == "brad_plex" and seen[-1]["length"] == "5"
    everyone = TautulliRecentHistoryTool(client).run()
    assert isinstance(everyone, list) and "user" not in seen[-1]
