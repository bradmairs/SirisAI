"""Ollama-backed LLM client.

The first (and so far only) concrete `LLMClient`. Nothing outside this
module should import `httpx` for LLM calls or know Ollama's request/response
shape -- that's the point of the interface in `app/llm/base.py`.
"""

from typing import Any

import httpx

from app.llm.base import LLMClient, LLMResponse, ToolCall


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

    async def aclose(self) -> None:
        await self._client.aclose()
