"""A hosted model over the OpenAI-compatible chat API (7.27).

Most providers speak it (OpenAI, OpenRouter, Groq, Together, Mistral, a
vLLM or LM Studio server...), so one client covers them: point
SIRISAI_CLOUD_LLM_BASE_URL at the provider's /v1 and set the model and key.
Used only by app/llm/cloud.py's rules, never by default.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from app.llm.base import LLMClient, LLMResponse, ToolCall


def to_openai(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """SirisAI's history (tool calls with dict arguments, no ids) in the API's shape."""
    out: list[dict[str, Any]] = []
    pending: list[str] = []
    n = 0
    for m in messages:
        role = m.get("role")
        if role == "assistant" and m.get("tool_calls"):
            calls = []
            for call in m["tool_calls"]:
                n += 1
                fn = call.get("function") or {}
                args = fn.get("arguments")
                calls.append({"id": f"call_{n}", "type": "function",
                              "function": {"name": fn.get("name"), "arguments": args if isinstance(args, str) else json.dumps(args or {})}})
            pending = [c["id"] for c in calls]
            out.append({"role": "assistant", "content": m.get("content") or None, "tool_calls": calls})
        elif role == "tool":
            out.append({"role": "tool", "tool_call_id": pending.pop(0) if pending else f"call_{n or 1}", "content": str(m.get("content") or "")})
        else:
            out.append({"role": role, "content": m.get("content") or ""})
    return out


class OpenAICompatibleLLMClient(LLMClient):
    def __init__(self, base_url: str, api_key: str, model: str, timeout: float = 90.0, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.model = model
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"), timeout=timeout, transport=transport,
            headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
        )

    async def generate(self, messages: list[dict[str, Any]], *, tools: list[dict[str, Any]] | None = None, **kwargs: object) -> LLMResponse:
        body: dict[str, Any] = {"model": self.model, "messages": to_openai(messages)}
        if tools:
            body["tools"] = tools
        response = await self._client.post("/chat/completions", json=body)
        response.raise_for_status()
        data = response.json()
        message = (data.get("choices") or [{}])[0].get("message") or {}
        calls = []
        for call in message.get("tool_calls") or []:
            fn = call.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            calls.append(ToolCall(name=fn.get("name") or "", arguments=args if isinstance(args, dict) else {}))
        usage = data.get("usage") or {}
        return LLMResponse(text=message.get("content") or None, tool_calls=calls,
                           prompt_tokens=usage.get("prompt_tokens"), completion_tokens=usage.get("completion_tokens"))

    async def aclose(self) -> None:
        await self._client.aclose()
