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

from app.conversation.service import ConversationStore
from app.core.config import Settings, get_settings
from app.db.session import SessionLocal
from app.home_assistant.client import HomeAssistantClient
from app.llm.base import LLMClient
from app.llm.factory import build_llm_client
from app.memory.service import MemoryStore
from app.plugins.base import ToolRegistry
from app.plugins.builtin.echo import EchoTool
from app.plugins.builtin.home_assistant import (
    HomeAssistantCallServiceTool,
    HomeAssistantGetStateTool,
    HomeAssistantListEntitiesTool,
)
from app.plugins.builtin.memory import RecallTool, RememberTool
from app.voice.base import SpeechToTextClient, TextToSpeechClient
from app.voice.factory import build_stt_client, build_tts_client


@dataclass
class SirisAI:
    """The core service. Thin today, the anchor point for everything later."""

    settings: Settings
    llm: LLMClient | None = None
    stt: SpeechToTextClient | None = None
    tts: TextToSpeechClient | None = None
    tools: ToolRegistry = field(default_factory=ToolRegistry)
    memory: MemoryStore = field(default_factory=lambda: MemoryStore(SessionLocal))
    conversation: ConversationStore = field(default_factory=lambda: ConversationStore(SessionLocal))

    def __post_init__(self) -> None:
        if self.llm is None:
            self.llm = build_llm_client(self.settings)
        if self.stt is None:
            self.stt = build_stt_client(self.settings)
        if self.tts is None:
            self.tts = build_tts_client(self.settings)
        self._home_assistant_client: HomeAssistantClient | None = None
        self._register_builtin_tools()

    def _register_builtin_tools(self) -> None:
        self.tools.register(EchoTool())
        self.tools.register(RememberTool(self.memory))
        self.tools.register(RecallTool(self.memory))

        if self.settings.home_assistant_base_url and self.settings.home_assistant_token:
            self._home_assistant_client = HomeAssistantClient(
                base_url=self.settings.home_assistant_base_url,
                token=self.settings.home_assistant_token,
            )
            self.tools.register(HomeAssistantListEntitiesTool(self._home_assistant_client))
            self.tools.register(HomeAssistantGetStateTool(self._home_assistant_client))
            self.tools.register(HomeAssistantCallServiceTool(self._home_assistant_client))

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
            "stt_provider": self.settings.stt_provider,
            "tts_provider": self.settings.tts_provider,
            "tools": [tool.name for tool in self.tools.list()],
        }

    async def aclose(self) -> None:
        """Release resources held by the core service (e.g. the LLM/STT/TTS
        clients' connection pools). Call once, at app shutdown."""
        assert self.llm is not None
        assert self.stt is not None
        assert self.tts is not None
        await self.llm.aclose()
        await self.stt.aclose()
        await self.tts.aclose()
        if self._home_assistant_client is not None:
            self._home_assistant_client.close()


@lru_cache
def get_siris() -> SirisAI:
    """Cached accessor for the shared SirisAI instance -- use as a FastAPI dependency."""
    return SirisAI(settings=get_settings())
