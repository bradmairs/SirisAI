"""Model-agnostic LLM client interface.

The rest of the app (SirisAI core, tools, endpoints) should only ever talk to
this interface, never to a specific provider's SDK/HTTP API. That keeps
swapping or adding backends (Ollama, a hosted API, a local llama.cpp server,
...) a matter of writing one new class here, not touching call sites.

Tool calling is expressed with a provider-agnostic shape too: `generate()`
takes tool schemas as plain dicts (the widely-used OpenAI-style function
format -- Ollama, and most hosted APIs, already speak it) and returns an
`LLMResponse` with either text or a list of `ToolCall`s. Callers never see a
provider's raw response shape.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolCall:
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass
class LLMResponse:
    text: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)


class LLMClient(ABC):
    """Common interface every model backend implements."""

    @abstractmethod
    async def generate(
        self,
        prompt: str,
        *,
        tools: list[dict[str, Any]] | None = None,
        **kwargs: object,
    ) -> LLMResponse:
        """Return a completion for `prompt`, optionally offering `tools` (in
        OpenAI function-calling schema) the model may choose to call instead
        of answering directly. Raises on backend failure."""
        raise NotImplementedError

    async def aclose(self) -> None:
        """Release any resources (connections, etc). No-op by default."""


class NullLLMClient(LLMClient):
    """Placeholder client used until a real backend is configured."""

    async def generate(
        self,
        prompt: str,
        *,
        tools: list[dict[str, Any]] | None = None,
        **kwargs: object,
    ) -> LLMResponse:
        raise RuntimeError("No LLM backend configured yet.")
