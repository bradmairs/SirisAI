"""Ollama-backed LLM client.

The first (and so far only) concrete `LLMClient`. Nothing outside this
module should import `httpx` for LLM calls or know Ollama's request/response
shape -- that's the point of the interface in `app/llm/base.py`.
"""

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx

from app.llm.base import LLMChunk, LLMClient, LLMResponse, ToolCall


logger = logging.getLogger(__name__)


def _retryable(exc: Exception) -> bool:
    """Worth trying the next Ollama machine: it's unreachable, timed out, or
    broke (5xx). A 4xx (an unknown model, a bad request) would fail there too."""
    if isinstance(exc, httpx.TransportError):
        return True
    return isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code >= 500


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
        assert body is not None
        message = body["message"]

        tool_calls = [
            ToolCall(name=call["function"]["name"], arguments=call["function"].get("arguments") or {})
            for call in message.get("tool_calls") or []
        ]
        # Token usage (milestone 47): Ollama reports these on a non-streamed
        # /api/chat response as prompt_eval_count/eval_count -- absent (not
        # zero) if the backend didn't include them, which LLMResponse's
        # Optional fields distinguish from "used zero tokens".
        return LLMResponse(
            text=message.get("content") or None,
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

        yield LLMChunk(
            response=LLMResponse(
                text="".join(text_parts) or None,
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
