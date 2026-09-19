"""One place that turns "call the model, and report progress as it goes" into
a plain awaitable, shared by Cortex and Planner so neither has to know how
streaming works.

With no `on_event` this is exactly `llm_client.generate(...)`. With one, the
model's reasoning and answer text are forwarded to it as they arrive
(`{"type": "thinking" | "content", "delta": str}`), and the fully assembled
LLMResponse is still returned at the end, so callers' logic is identical
either way. A client with no `stream_generate` (a test fake, a backend that
can't stream) just falls back to generate().
"""

from collections.abc import Awaitable, Callable
from typing import Any

from app.llm.base import LLMResponse

EventCallback = Callable[[dict[str, Any]], Awaitable[None]]


async def generate_with_events(
    llm_client: Any, messages: list[dict[str, Any]], on_event: EventCallback | None, **kwargs: Any
) -> LLMResponse:
    stream = getattr(llm_client, "stream_generate", None)
    if on_event is None or stream is None:
        response = await llm_client.generate(messages, **kwargs)
        if on_event is not None and response.text:
            await on_event({"type": "content", "delta": response.text})
        return response

    final: LLMResponse | None = None
    async for chunk in stream(messages, **kwargs):
        if chunk.thinking:
            await on_event({"type": "thinking", "delta": chunk.thinking})
        if chunk.content:
            await on_event({"type": "content", "delta": chunk.content})
        if chunk.response is not None:
            final = chunk.response
    return final if final is not None else LLMResponse()
