"""POST /siris/voice/converse: a spoken turn streamed back sentence by
sentence, plus spoken yes/no confirmation (app/voice/)."""

import base64
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest

from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.siris import SirisAI, get_siris
from app.llm.base import LLMChunk, LLMResponse, ToolCall
from app.main import app
from app.voice.confirmations import classify_reply, describe_call, voice_confirmable
from app.voice.sentences import SentenceChunker, speakable, strip_spoken_prefix
from tests.test_permissions import _ConfirmRequiredTool

# -- SentenceChunker / speakable


def test_chunker_emits_each_sentence_as_soon_as_it_ends() -> None:
    chunker = SentenceChunker()
    assert chunker.feed("The kitchen light ") == []
    assert chunker.feed("is on. The hall") == ["The kitchen light is on."]
    assert chunker.feed(" light is off! Anything") == ["The hall light is off!"]
    assert chunker.flush() == ["Anything"]


def test_chunker_does_not_split_on_abbreviations_or_decimals() -> None:
    chunker = SentenceChunker()
    sentences = chunker.feed("Use e.g. the den. It is 21.5 degrees. Done")
    assert sentences == ["Use e.g. the den.", "It is 21.5 degrees."]


def test_chunker_splits_list_items_and_skips_code_blocks() -> None:
    chunker = SentenceChunker()
    text = "Here you go:\n- **Kitchen**: on\n- Hall: off\n```\nprint('x')\n```\nThat's all."
    assert chunker.feed(text) + chunker.flush() == ["Here you go:", "- **Kitchen**: on", "- Hall: off", "That's all."]


def test_chunker_tracks_consumed_prefix() -> None:
    chunker = SentenceChunker()
    chunker.feed("One. Two")
    assert chunker.consumed == "One. "


def test_speakable_strips_markdown() -> None:
    assert speakable("- **Kitchen** light: [details](http://x)") == "Kitchen light: details"
    assert speakable("## Status") == "Status"
    assert speakable("See https://example.com/foo now") == "See a link now"
    assert speakable("|---|---|") == ""


def test_strip_spoken_prefix_ignores_whitespace_and_returns_remainder() -> None:
    assert strip_spoken_prefix("It is on. ", "It is on.\n\nNote: nothing changed.") == "\n\nNote: nothing changed."
    assert strip_spoken_prefix("", "Whole answer.") == "Whole answer."
    assert strip_spoken_prefix("It is on. ", "It was off.") == "was off."


# -- confirmations


def test_classify_reply() -> None:
    assert classify_reply("Yes please") == "yes"
    assert classify_reply("go ahead") == "yes"
    assert classify_reply("No, cancel that") == "no"
    assert classify_reply("what's the weather") is None
    assert classify_reply("stop the music in the kitchen and the lounge please") is None


def test_describe_call_and_voice_confirmable() -> None:
    call = {"domain": "light", "service": "turn_on", "entity_id": "light.kitchen_main"}
    assert describe_call("home_assistant_call_service", call) == "turn on kitchen main"
    assert describe_call("docker_restart_container", {"container": "plex"}) == "docker restart container: plex"
    blocked = {"lock", "cover"}
    assert voice_confirmable("home_assistant_call_service", call, blocked)
    assert not voice_confirmable(
        "home_assistant_call_service", {"domain": "lock", "service": "unlock", "entity_id": "lock.front"}, blocked
    )
    assert not voice_confirmable(
        "home_assistant_call_service", {"domain": "homeassistant", "service": "turn_on", "entity_id": "cover.garage"}, blocked
    )


# -- the endpoint


class _FakeTTS:
    async def synthesize(self, text: str) -> bytes:
        return f"mp3:{text}".encode()

    async def aclose(self) -> None:
        pass


class _FakeSTT:
    def __init__(self, text: str) -> None:
        self.text = text

    async def transcribe(self, audio: bytes, *, filename: str = "audio.wav", content_type: str = "audio/wav") -> str:
        return self.text

    async def aclose(self) -> None:
        pass


class _ScriptedLLM:
    """Streams each scripted response; a ToolCall entry makes that call."""

    def __init__(self, *responses: Any) -> None:
        self.responses = list(responses)

    async def generate(self, messages: list[dict[str, Any]], *, tools: Any = None, **kwargs: object) -> LLMResponse:
        raise AssertionError("expected streaming")

    async def stream_generate(self, messages: list[dict[str, Any]], *, tools: Any = None, **kwargs: object) -> AsyncIterator[LLMChunk]:
        item = self.responses.pop(0)
        if isinstance(item, ToolCall):
            yield LLMChunk(response=LLMResponse(tool_calls=[item]))
            return
        for piece in item:
            yield LLMChunk(content=piece)
        yield LLMChunk(response=LLMResponse(text="".join(item)))

    async def aclose(self) -> None:
        pass


def _siris(llm: Any, stt_text: str | None = None) -> SirisAI:
    siris = SirisAI(settings=Settings(), llm=llm)
    siris.tts = _FakeTTS()  # type: ignore[assignment]
    if stt_text is not None:
        siris.stt = _FakeSTT(stt_text)  # type: ignore[assignment]
    return siris


def _converse(client: TestClient, siris: SirisAI, **data: Any) -> list[dict[str, Any]]:
    app.dependency_overrides[get_siris] = lambda: siris
    try:
        files = data.pop("files", None)
        response = client.post("/siris/voice/converse", data=data, files=files)
        assert response.status_code == 200, response.text
        assert response.headers["content-type"].startswith("application/x-ndjson")
        return [json.loads(line) for line in response.text.splitlines() if line.strip()]
    finally:
        app.dependency_overrides.pop(get_siris, None)


def test_converse_streams_sentences_with_audio_then_final(client: TestClient) -> None:
    siris = _siris(_ScriptedLLM(["The kitchen ", "light is on. ", "The hall ", "is dark."]), stt_text="what's on?")
    events = _converse(client, siris, files={"file": ("a.wav", b"RIFF....", "audio/wav")})

    assert events[0] == {"type": "transcript", "text": "what's on?"}
    sentences = [e for e in events if e["type"] == "sentence"]
    assert [s["text"] for s in sentences] == ["The kitchen light is on.", "The hall is dark."]
    assert base64.b64decode(sentences[0]["audio"]) == b"mp3:The kitchen light is on."
    final = events[-1]
    assert final["type"] == "final"
    assert final["response"] == "The kitchen light is on. The hall is dark."
    assert final["expects_reply"] is False
    # The first sentence was emitted before the answer finished streaming.
    assert events.index(sentences[0]) < events.index(final)


def test_converse_with_text_and_question_expects_reply(client: TestClient) -> None:
    siris = _siris(_ScriptedLLM(["Which room did you mean?"]))
    events = _converse(client, siris, text="turn on the light")
    assert events[0] == {"type": "transcript", "text": "turn on the light"}
    assert events[-1]["expects_reply"] is True


def test_converse_speaks_appended_correction_once(client: TestClient) -> None:
    # "turned on" with no state-changing tool run makes Cortex append a note
    # after streaming finished; only the note should be spoken extra.
    siris = _siris(_ScriptedLLM(["I turned on the lamp."]))
    events = _converse(client, siris, text="lamp on")
    texts = [e["text"] for e in events if e["type"] == "sentence"]
    assert texts[0] == "I turned on the lamp."
    assert texts.count("I turned on the lamp.") == 1
    assert any(t.startswith("Note:") for t in texts)


def test_converse_asks_for_confirmation_and_yes_runs_it(client: TestClient) -> None:
    llm = _ScriptedLLM(ToolCall(name="dangerous_action", arguments={"target": "reactor"}), ["Done, reactor handled."])
    siris = _siris(llm)
    siris.tools.register(_ConfirmRequiredTool())

    first = _converse(client, siris, text="handle the reactor")
    final = first[-1]
    assert final["confirmation_required"] == {"name": "dangerous_action", "arguments": {"target": "reactor"}}
    assert final["expects_reply"] is True
    assert "Should I go ahead?" in final["response"]
    conversation_id = final["conversation_id"]

    second = _converse(client, siris, text="yes", conversation_id=conversation_id)
    assert second[-1]["response"] == "Done, reactor handled."
    ran = siris.audit.find_recent("dangerous_action", limit=1)[0]
    assert ran["result"] == {"acted_on": "reactor"}
    assert ran["confirmation_required"] is False


def test_converse_no_cancels_a_pending_confirmation(client: TestClient) -> None:
    siris = _siris(_ScriptedLLM(ToolCall(name="dangerous_action", arguments={"target": "cancel-me"})))
    siris.tools.register(_ConfirmRequiredTool())
    first = _converse(client, siris, text="do the thing")
    second = _converse(client, siris, text="no", conversation_id=first[-1]["conversation_id"])
    assert second[-1]["response"] == "Okay, I won't do that."
    runs = siris.audit.find_recent("dangerous_action", argument_contains="cancel-me", limit=5)
    assert runs and all(r["result"] is None for r in runs)


def test_converse_blocked_domain_is_never_voice_confirmable(client: TestClient) -> None:
    call = ToolCall(name="dangerous_action", arguments={"target": "blocked-one"})
    siris = _siris(_ScriptedLLM(call, ["unused"]))
    siris.settings.voice_confirmation_blocked_domains = "dangerous_action"
    siris.tools.register(_ConfirmRequiredTool())
    first = _converse(client, siris, text="do it")
    assert first[-1]["expects_reply"] is False
    assert "confirm it in the SirisAI app" in first[-1]["response"]
    second_llm_turn = _converse(client, siris, text="yes", conversation_id=first[-1]["conversation_id"])
    # "yes" is now just a new message, not an approval.
    runs = siris.audit.find_recent("dangerous_action", argument_contains="blocked-one", limit=5)
    assert all(r["result"] is None for r in runs)
    assert second_llm_turn[-1]["response"] == "unused"


def test_converse_without_input_is_422(client: TestClient) -> None:
    response = client.post("/siris/voice/converse", data={})
    assert response.status_code == 422


def test_converse_with_audio_but_no_stt_is_501(client: TestClient) -> None:
    siris = SirisAI(settings=Settings())
    app.dependency_overrides[get_siris] = lambda: siris
    try:
        response = client.post("/siris/voice/converse", files={"file": ("a.wav", b"x", "audio/wav")})
        assert response.status_code == 501
    finally:
        app.dependency_overrides.pop(get_siris, None)


@pytest.mark.parametrize("reply, expected", [
    ("The lounge lights are on. Shall I turn them off?", True),
    ("Shall I turn them off? Just say the word.", True),
    ('Did you mean the office or the lounge?"', True),
    ("Want me to check the NAS too?)", True),
    ("It's 7pm. Anything else? I'll keep an eye on the server overnight and let you know if it changes.", False),
    ("Done, the garage door is closed.", False),
    ("Why does this happen? Usually a tripped breaker.", False),
    ("", False),
])
def test_a_reply_that_asks_something_expects_an_answer(reply: str, expected: bool) -> None:
    from app.voice.sentences import expects_answer
    assert expects_answer(reply) is expected
