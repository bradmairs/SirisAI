"""One place that turns "call the model, and report progress as it goes" into
a plain awaitable, shared by Cortex and Planner so neither has to know how
streaming works.

With no `on_event` this is exactly `llm_client.generate(...)`. With one, the
model's reasoning and answer text are forwarded to it as they arrive
(`{"type": "thinking" | "content", "delta": str}`), and the fully assembled
LLMResponse is still returned at the end, so callers' logic is identical
either way. A client with no `stream_generate` (a test fake, a backend that
can't stream) just falls back to generate().

**Tool calls written as text (6.69).** Smaller models sometimes write a tool
call into their answer instead of making it: the whole reply
(`{"name": "brain_get_note", "parameters": {...}}`), or after a sentence
("Let me try filtering again. {"name": ...}"). Streamed, that JSON reached
the chat and the voice before anything could tell it was a call. Now, once
the streamed answer reaches a `{` or a code fence, the rest is held back
until the reply is complete. If it ends in a tool call, the call is taken
out of the text and returned as a real tool call (whatever tool it names;
an unknown one fails like any failed tool, and the model answers), and only
the words before it are shown. Otherwise the held text is sent as usual.
"""

import json
import re
from collections.abc import Awaitable, Callable
from typing import Any

from app.llm.base import LLMResponse, ToolCall

EventCallback = Callable[[dict[str, Any]], Awaitable[None]]
_NAME = re.compile(r"^[a-z][a-z0-9_]{2,63}$")
_HOLD_FROM = re.compile(r"\{|```")


def written_call(text: str | None) -> tuple[str, ToolCall] | None:
    """(the words before it, the call) when `text` ends in a tool call written as JSON."""
    if not text or "{" not in text:
        return None
    decoder = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch != "{":
            continue
        try:
            data, end = decoder.raw_decode(text, i)
        except ValueError:
            continue
        if not isinstance(data, dict) or not _NAME.match(str(data.get("name") or "")):
            continue
        arguments = data.get("parameters", data.get("arguments", {}))
        if not isinstance(arguments, dict) or set(data) - {"name", "parameters", "arguments", "type"}:
            continue
        if text[end:].strip(" \n\t`.") != "":
            continue  # more answer after it: it's an example, not a call
        before = re.sub(r"```(?:json)?\s*$", "", text[:i]).rstrip()
        return before, ToolCall(name=data["name"], arguments=arguments)
    return None


def _take_calls(response: LLMResponse) -> LLMResponse:
    if response.tool_calls:
        return response
    found = written_call(response.text)
    if found is None:
        return response
    before, call = found
    return LLMResponse(text=before or None, tool_calls=[call], prompt_tokens=response.prompt_tokens,
                       completion_tokens=response.completion_tokens)


async def generate_with_events(
    llm_client: Any, messages: list[dict[str, Any]], on_event: EventCallback | None, **kwargs: Any
) -> LLMResponse:
    stream = getattr(llm_client, "stream_generate", None)
    if on_event is None or stream is None:
        response = _take_calls(await llm_client.generate(messages, **kwargs))
        if on_event is not None and response.text:
            await on_event({"type": "content", "delta": response.text})
        return response

    final: LLMResponse | None = None
    sent = ""  # what's been shown so far
    held: list[str] = []  # from the first `{` or code fence on: shown only once it's known not to be a call
    async for chunk in stream(messages, **kwargs):
        if chunk.thinking:
            await on_event({"type": "thinking", "delta": chunk.thinking})
        if chunk.content:
            if held:
                held.append(chunk.content)
            else:
                m = _HOLD_FROM.search(chunk.content)
                show, rest = (chunk.content[: m.start()], chunk.content[m.start():]) if m else (chunk.content, "")
                if show:
                    sent += show
                    await on_event({"type": "content", "delta": show})
                if rest:
                    held.append(rest)
        if chunk.response is not None:
            final = chunk.response
    final = _take_calls(final if final is not None else LLMResponse(text=(sent + "".join(held)) or None))
    shown_text = final.text or ""
    if shown_text.startswith(sent) and len(shown_text) > len(sent):
        await on_event({"type": "content", "delta": shown_text[len(sent):]})
    return final
