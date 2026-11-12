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


def build_llm_client(
    provider: str, ollama_base_url: str, ollama_model: str, keep_alive: str | None = None,
    think: bool | None = None, fallback_base_urls: list[str] | None = None,
) -> LLMClient:
    if provider == "ollama":
        return OllamaClient(base_url=ollama_base_url, model=ollama_model, keep_alive=keep_alive,
                            think=think, fallback_base_urls=fallback_base_urls)
    return NullLLMClient()


def think_setting(value: str) -> bool | None:
    """SIRISAI_*_THINK: "on"/"off", or "" for the model's own default."""
    v = (value or "").strip().lower()
    return True if v in ("on", "true", "1", "yes") else False if v in ("off", "false", "0", "no") else None
