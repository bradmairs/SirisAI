"""Model-agnostic LLM client interface.

The rest of the app (SirisAI core, tools, endpoints) should only ever talk to
this interface, never to a specific provider's SDK/HTTP API. That keeps
swapping or adding backends (Ollama, a hosted API, a local llama.cpp server,
...) a matter of writing one new class here, not touching call sites.

No concrete implementation ships yet -- an Ollama-backed client is the next
milestone. `NullLLMClient` lets the app boot and be exercised end-to-end
before a real backend is wired in.
"""

from abc import ABC, abstractmethod


class LLMClient(ABC):
    """Common interface every model backend implements."""

    @abstractmethod
    async def generate(self, prompt: str, **kwargs: object) -> str:
        """Return a completion for `prompt`. Raises on backend failure."""
        raise NotImplementedError

    async def aclose(self) -> None:
        """Release any resources (connections, etc). No-op by default."""


class NullLLMClient(LLMClient):
    """Placeholder client used until a real backend is configured."""

    async def generate(self, prompt: str, **kwargs: object) -> str:
        raise RuntimeError("No LLM backend configured yet.")
