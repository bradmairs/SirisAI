"""Ollama-backed LLM client.

The first (and so far only) concrete `LLMClient`. Nothing outside this
module should import `httpx` for LLM calls or know Ollama's request/response
shape -- that's the point of the interface in `app/llm/base.py`.
"""

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from app.llm.base import LLMChunk, LLMClient, LLMResponse, ToolCall


class OllamaClient(LLMClient):
    def __init__(
        self,
        base_url: str,
        model: str,
        timeout: float = 60.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.model = model
        self._client = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout, transport=transport)

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
            **kwargs,
        }
        if tools:
            payload["tools"] = tools

        response = await self._client.post("/api/chat", json=payload)
        response.raise_for_status()
        body = response.json()
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
            **kwargs,
        }
        if tools:
            payload["tools"] = tools

        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        prompt_tokens: int | None = None
        completion_tokens: int | None = None

        async with self._client.stream("POST", "/api/chat", json=payload) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line.strip():
                    continue
                body = json.loads(line)
                message = body.get("message") or {}
                thinking = message.get("thinking") or ""
                content = message.get("content") or ""
                for call in message.get("tool_calls") or []:
                    tool_calls.append(
                        ToolCall(name=call["function"]["name"], arguments=call["function"].get("arguments") or {})
                    )
                if content:
                    text_parts.append(content)
                if thinking or content:
                    yield LLMChunk(thinking=thinking, content=content)
                if body.get("done"):
                    prompt_tokens = body.get("prompt_eval_count")
                    completion_tokens = body.get("eval_count")

        yield LLMChunk(
            response=LLMResponse(
                text="".join(text_parts) or None,
                tool_calls=tool_calls,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            )
        )

    async def aclose(self) -> None:
        await self._client.aclose()
