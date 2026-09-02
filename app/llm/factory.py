"""Picks the configured LLM backend.

This is the one place that knows about every concrete `LLMClient`. Adding a
new provider means adding a case here and a class next to `ollama.py` --
nothing else in the app changes.
"""

from app.core.config import Settings
from app.llm.base import LLMClient, NullLLMClient
from app.llm.ollama import OllamaClient


def build_llm_client(settings: Settings) -> LLMClient:
    if settings.llm_provider == "ollama":
        return OllamaClient(base_url=settings.ollama_base_url, model=settings.ollama_model)
    return NullLLMClient()
