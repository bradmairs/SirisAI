"""Ollama-backed LLM client.

The first (and so far only) concrete `LLMClient`. Nothing outside this
module should import `httpx` for LLM calls or know Ollama's request/response
shape -- that's the point of the interface in `app/llm/base.py`.
"""

import json
import logging
from collections import deque
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import httpx

from app.llm.base import LLMChunk, LLMClient, LLMResponse, ToolCall


# Each switch to a fallback Ollama machine (6.48, shown on SirisAI's health page).
FALLBACKS: deque[dict[str, str]] = deque(maxlen=200)
logger = logging.getLogger(__name__)


def _retryable(exc: Exception) -> bool:
    """Worth trying the next Ollama machine: it's unreachable, timed out, or
    broke (5xx). A 4xx (an unknown model, a bad request) would fail there too."""
    if isinstance(exc, httpx.TransportError):
        return True
    return isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code >= 500



def text_tool_call(text: str | None, tools: list[dict[str, Any]] | None) -> ToolCall | None:
    """A tool call the model wrote as its answer instead of making it (6.05).
    llama3.1 sometimes replies with just `{"name": "weather_current",
    "parameters": {}}` as text, which was then shown or spoken as the answer
    (found by the nightly self-test). Taken as a call only when the whole
    reply is that one JSON object (code fences allowed) naming a tool that
    was offered this turn; anything else stays text."""
    if not text or not tools:
        return None
    body = text.strip()
    if body.startswith("```"):
        body = body.strip("`").removeprefix("json").strip()
    if not (body.startswith("{") and body.endswith("}")):
        return None
    try:
        data = json.loads(body)
    except ValueError:
        return None
    offered = {(t.get("function") or {}).get("name") for t in tools}
    arguments = data.get("parameters", data.get("arguments", {})) if isinstance(data, dict) else None
    if not isinstance(data, dict) or data.get("name") not in offered or not isinstance(arguments, dict) \
            or set(data) - {"name", "parameters", "arguments", "type"}:
        return None
    return ToolCall(name=data["name"], arguments=arguments)

class OllamaClient(LLMClient):
    def __init__(
        self,
        base_url: str,
        model: str,
        timeout: float = 60.0,
        transport: httpx.BaseTransport | None = None,
        keep_alive: str | None = None,
        think: bool | None = None,
        fallback_base_urls: list[str] | None = None,
    ) -> None:
        self.model = model
        # How long Ollama keeps the model in memory after a call (4.6).
        # Ollama's own default is 5 minutes, after which the next question
        # waits seconds while the model loads again.
        self.keep_alive = keep_alive or None
        # Reasoning models (gemma4, qwen3.5) think before every answer unless
        # told not to: measured 1.9 s against 0.09 s for a one-line reply.
        # None leaves the model's default.
        self.think = think
        # The first address is tried first; the rest only when it's down or
        # broken (5.8: a second AI machine, either way round).
        urls = [base_url, *(fallback_base_urls or [])]
        self._clients = [
            httpx.AsyncClient(base_url=u.rstrip("/"), timeout=timeout, transport=transport)
            for u in dict.fromkeys(u for u in urls if u)
        ]
        self._client = self._clients[0]

    async def generate(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        **kwargs: object,
    ) -> LLMResponse:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            **self._keep(),
            **kwargs,
        }
        if tools:
            payload["tools"] = tools

        body = None
        for i, client in enumerate(self._clients):
            try:
                response = await client.post("/api/chat", json=payload)
                response.raise_for_status()
                body = response.json()
                break
            except Exception as exc:
                if i == len(self._clients) - 1 or not _retryable(exc):
                    raise
                logger.warning("Ollama at %s failed (%s); trying %s", client.base_url, exc, self._clients[i + 1].base_url)
                FALLBACKS.append({"at": datetime.now(UTC).isoformat(), "from": str(client.base_url), "to": str(self._clients[i + 1].base_url),
                                  "why": f"{type(exc).__name__}: {exc}"[:160]})  # 6.48: for the health page
        assert body is not None
        message = body["message"]

        tool_calls = [
            ToolCall(name=call["function"]["name"], arguments=call["function"].get("arguments") or {})
            for call in message.get("tool_calls") or []
        ]
        text = message.get("content") or None
        if not tool_calls and (written := text_tool_call(text, tools)) is not None:
            tool_calls, text = [written], None
        # Token usage (milestone 47): Ollama reports these on a non-streamed
        # /api/chat response as prompt_eval_count/eval_count -- absent (not
        # zero) if the backend didn't include them, which LLMResponse's
        # Optional fields distinguish from "used zero tokens".
        return LLMResponse(
            text=text,
            tool_calls=tool_calls,
            prompt_tokens=body.get("prompt_eval_count"),
            completion_tokens=body.get("eval_count"),
        )

    async def stream_generate(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        **kwargs: object,
    ) -> AsyncIterator[LLMChunk]:
        """Streamed /api/chat: Ollama sends newline-delimited JSON, each
        line a small `message` delta -- `thinking` (the model's reasoning,
        for models that emit it) and/or `content` (the answer) -- with tool
        calls arriving whole on whichever line the model finishes one, and
        token counts on the final `done` line."""
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            **self._keep(),
            **kwargs,
        }
        if tools:
            payload["tools"] = tools

        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        prompt_tokens: int | None = None
        completion_tokens: int | None = None

        for i, client in enumerate(self._clients):
            started = False
            try:
                async with client.stream("POST", "/api/chat", json=payload) as response:
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        if not line.strip():
                            continue
                        body = json.loads(line)
                        message = body.get("message") or {}
                        thinking = message.get("thinking") or ""
                        content = message.get("content") or ""
                        for call in message.get("tool_calls") or []:
                            started = True
                            tool_calls.append(
                                ToolCall(name=call["function"]["name"], arguments=call["function"].get("arguments") or {})
                            )
                        if content:
                            text_parts.append(content)
                        if thinking or content:
                            started = True
                            yield LLMChunk(thinking=thinking, content=content)
                        if body.get("done"):
                            prompt_tokens = body.get("prompt_eval_count")
                            completion_tokens = body.get("eval_count")
                break
            except Exception as exc:
                # Switch machines only before anything was said: never half an answer from each.
                if started or i == len(self._clients) - 1 or not _retryable(exc):
                    raise
                logger.warning("Ollama at %s failed (%s); trying %s", client.base_url, exc, self._clients[i + 1].base_url)

        text = "".join(text_parts) or None
        if not tool_calls and (written := text_tool_call(text, tools)) is not None:
            tool_calls, text = [written], None
        yield LLMChunk(
            response=LLMResponse(
                text=text,
                tool_calls=tool_calls,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            )
        )

    def _keep(self) -> dict[str, Any]:
        out: dict[str, Any] = {"keep_alive": self.keep_alive} if self.keep_alive else {}
        if self.think is not None:
            out["think"] = self.think
        return out

    async def warm(self) -> None:
        """Load the model (or keep it loaded) without generating anything:
        Ollama treats a /api/generate with no prompt as a preload."""
        keep = {"keep_alive": self.keep_alive} if self.keep_alive else {}
        response = await self._client.post("/api/generate", json={"model": self.model, **keep})
        response.raise_for_status()

    async def aclose(self) -> None:
        for client in self._clients:
            await client.aclose()
