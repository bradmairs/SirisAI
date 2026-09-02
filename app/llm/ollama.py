"""Ollama-backed LLM client.

The first (and so far only) concrete `LLMClient`. Nothing outside this
module should import `httpx` for LLM calls or know Ollama's request/response
shape -- that's the point of the interface in `app/llm/base.py`.
"""

import httpx

from app.llm.base import LLMClient


class OllamaClient(LLMClient):
    def __init__(self, base_url: str, model: str, timeout: float = 60.0) -> None:
        self.model = model
        self._client = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout)

    async def generate(self, prompt: str, **kwargs: object) -> str:
        response = await self._client.post(
            "/api/generate",
            json={"model": self.model, "prompt": prompt, "stream": False, **kwargs},
        )
        response.raise_for_status()
        return response.json()["response"]

    async def aclose(self) -> None:
        await self._client.aclose()
