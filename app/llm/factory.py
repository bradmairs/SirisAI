"""Picks a configured LLM backend.

This is the one place that knows about every concrete `LLMClient`. Adding a
new provider means adding a case here and a class next to `ollama.py` --
nothing else in the app changes.

Takes plain params rather than `Settings` directly so it can build more
than one client from different settings blocks -- see app/llm/router.py,
which uses this for both the default ("general") model and an optional
second ("fast") one.
"""

from app.llm.base import LLMClient, NullLLMClient
from app.llm.ollama import OllamaClient


def build_llm_client(provider: str, ollama_base_url: str, ollama_model: str) -> LLMClient:
    if provider == "ollama":
        return OllamaClient(base_url=ollama_base_url, model=ollama_model)
    return NullLLMClient()
