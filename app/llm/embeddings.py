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

import threading

import httpx


_CACHE_SIZE = 64


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
        self._cache: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def embed(self, text: str) -> list[float]:
        # The same message is embedded twice a turn -- once to match
        # memories, once to pick tools -- so recent ones are kept (4.6).
        cached = self._cache.get(text)
        if cached is not None:
            return cached
        response = self._client.post("/api/embed", json={"model": self.model, "input": text})
        response.raise_for_status()
        embeddings = response.json()["embeddings"]
        self._remember(text, embeddings[0])
        return embeddings[0]

    def _remember(self, text: str, vector: list[float]) -> None:
        with self._lock:
            self._cache[text] = vector
            while len(self._cache) > _CACHE_SIZE:
                self._cache.pop(next(iter(self._cache)))

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        """One request for many texts (Ollama's /api/embed takes a list)."""
        if len(texts) == 1 and texts[0] in self._cache:
            return [self._cache[texts[0]]]
        response = self._client.post("/api/embed", json={"model": self.model, "input": texts})
        response.raise_for_status()
        vectors = response.json()["embeddings"]
        if len(texts) == 1 and vectors:
            self._remember(texts[0], vectors[0])
        return vectors

    def close(self) -> None:
        self._client.close()
