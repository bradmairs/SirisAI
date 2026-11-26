"""4.27.1: fixes from the live alerts of 22-26 Nov 2026."""

from __future__ import annotations

import httpx

from app.core import tool_args
from app.llm.ollama import repaired_call, text_tool_call
from app.tautulli.client import TautulliClient


def test_numbers_and_yes_no_written_as_text_become_what_the_tool_asks_for():
    schema = {"properties": {"length": {"type": "integer"}, "temp": {"type": "number"}, "on": {"type": "boolean"}, "user": {"type": "string"}}}
    got = tool_args.coerce({"length": "5", "temp": "21.5", "on": "true", "user": "5"}, schema)
    assert got == {"length": 5, "temp": 21.5, "on": True, "user": "5"}
    assert tool_args.coerce({"length": 5.0}, schema) == {"length": 5}
    assert tool_args.coerce({"length": "five", "temp": None}, schema) == {"length": "five", "temp": None}  # left for the tool
    assert tool_args.coerce({"x": 1}, None) == {"x": 1}


def test_the_plex_history_question_no_longer_fails_on_a_written_number():
    seen = []

    def handler(request):
        seen.append(dict(request.url.params))
        return httpx.Response(200, json={"response": {"data": {"data": [{"full_title": "Theseus", "user": "brad", "date": 2}]}}})

    client = TautulliClient("http://tautulli", "secret-key", transport=httpx.MockTransport(handler))
    assert client.recent_history(length="5", user_ids=[1])[0]["title"] == "Theseus"  # type: ignore[arg-type]


def test_tautullis_key_never_appears_in_an_error():
    client = TautulliClient("http://tautulli", "secret-key", transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    try:
        client.recent_history()
    except RuntimeError as exc:
        assert "secret-key" not in str(exc) and "<key>" in str(exc)
    else:
        raise AssertionError("expected a failure")


def test_a_tool_call_written_with_broken_json_is_still_made():
    tools = [{"function": {"name": "get_current_time"}}]
    call = text_tool_call('{"name": "get_current_time", "parameters": {"}}', tools)
    assert call is not None and (call.name, call.arguments) == ("get_current_time", {})
    assert text_tool_call('{"name": "rm_everything", "parameters": {"}}', tools) is None  # still only tools offered
    assert repaired_call('{"name": "x_y", "parameters": {"long": "garbled text here}}') is None  # not guessed at
