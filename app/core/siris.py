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
from typing import Any

from app.core.config import Settings, get_settings
from app.db.session import SessionLocal
from app.llm.base import LLMClient
from app.llm.factory import build_llm_client
from app.memory.service import MemoryStore
from app.plugins.base import ToolRegistry
from app.plugins.builtin.echo import EchoTool
from app.plugins.builtin.memory import RecallTool, RememberTool


@dataclass
class SirisAI:
    """The core service. Thin today, the anchor point for everything later."""

    settings: Settings
    llm: LLMClient | None = None
    tools: ToolRegistry = field(default_factory=ToolRegistry)
    memory: MemoryStore = field(default_factory=lambda: MemoryStore(SessionLocal))

    def __post_init__(self) -> None:
        if self.llm is None:
            self.llm = build_llm_client(self.settings)
        self._register_builtin_tools()

    def _register_builtin_tools(self) -> None:
        self.tools.register(EchoTool())
        self.tools.register(RememberTool(self.memory))
        self.tools.register(RecallTool(self.memory))

    def run_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        """Look up and invoke a registered tool by name. Raises ValueError
        if no tool with that name is registered."""
        tool = self.tools.get(name)
        if tool is None:
            raise ValueError(f"Unknown tool: {name}")
        return tool.run(**arguments)

    def status(self) -> dict[str, object]:
        """A snapshot of the core service's state, for the status endpoint."""
        return {
            "app_name": self.settings.app_name,
            "environment": self.settings.environment,
            "llm_provider": self.settings.llm_provider,
            "tools": [tool.name for tool in self.tools.list()],
        }

    async def aclose(self) -> None:
        """Release resources held by the core service (e.g. the LLM client's
        connection pool). Call once, at app shutdown."""
        assert self.llm is not None
        await self.llm.aclose()


@lru_cache
def get_siris() -> SirisAI:
    """Cached accessor for the shared SirisAI instance -- use as a FastAPI dependency."""
    return SirisAI(settings=get_settings())
