"""Text-embedding client, for semantic memory recall (app/memory/service.py).

A separate concern from `LLMClient` (app/llm/base.py) -- embeddings and
chat completions are different model families and different Ollama API
endpoints (`/api/embed` vs `/api/chat`), and callers that only want a
vector for similarity search shouldn't have to pull in the whole
tool-calling chat interface to get one.

Synchronous on purpose, unlike `OllamaClient`: everything that calls this
(`MemoryStore.remember()`/`recall()`) is itself synchronous -- called from
`Tool.run()`, which `SirisAI.run_tool()` already runs on a worker thread
via `asyncio.to_thread()` (see app/cortex/orchestrator.py's module
docstring), the same pattern every other tool-invoked HTTP client here
(UniFiClient, HomeAssistantClient, ...) already follows.
"""

import math

import httpx


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """1.0 = identical direction (same meaning), 0.0 = unrelated, negative =
    opposite -- the standard measure for comparing two embedding vectors.
    Shared by MemoryStore (recall ranking, write-time dedup) and
    self_maintenance_check (duplicate-memory detection) rather than each
    reimplementing it."""
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


class OllamaEmbeddingClient:
    def __init__(
        self,
        base_url: str,
        model: str,
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.model = model
        self._client = httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout, transport=transport)

    def embed(self, text: str) -> list[float]:
        response = self._client.post("/api/embed", json={"model": self.model, "input": text})
        response.raise_for_status()
        embeddings = response.json()["embeddings"]
        return embeddings[0]

    def close(self) -> None:
        self._client.close()
