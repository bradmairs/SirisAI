"""The SirisAI core service.

Everything in the app -- endpoints, tools, the LLM backend, memory -- hangs
off a single `SirisAI` instance. Right now it's thin (settings + a tool
registry + a placeholder LLM client), but new subsystems (memory, an LLM
tool-use loop, home automation clients, ...) attach here rather than
spreading global state across modules.

FastAPI gets a single shared instance via `get_siris()`, used as a
dependency, so routes never construct their own.
"""

from dataclasses import dataclass, field
from functools import lru_cache

from app.core.config import Settings, get_settings
from app.llm.base import LLMClient, NullLLMClient
from app.plugins.base import ToolRegistry
from app.plugins.builtin.echo import EchoTool


@dataclass
class SirisAI:
    """The core service. Thin today, the anchor point for everything later."""

    settings: Settings
    llm: LLMClient = field(default_factory=NullLLMClient)
    tools: ToolRegistry = field(default_factory=ToolRegistry)

    def __post_init__(self) -> None:
        self._register_builtin_tools()

    def _register_builtin_tools(self) -> None:
        self.tools.register(EchoTool())

    def status(self) -> dict[str, object]:
        """A snapshot of the core service's state, for the status endpoint."""
        return {
            "app_name": self.settings.app_name,
            "environment": self.settings.environment,
            "llm_provider": self.settings.llm_provider,
            "tools": [tool.name for tool in self.tools.list()],
        }


@lru_cache
def get_siris() -> SirisAI:
    """Cached accessor for the shared SirisAI instance -- use as a FastAPI dependency."""
    return SirisAI(settings=get_settings())
