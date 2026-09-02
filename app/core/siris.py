"""The SirisAI core service.

Everything in the app -- endpoints, tools, the LLM backend, memory -- hangs
off a single `SirisAI` instance. Right now it's thin (settings + a tool
registry + a placeholder LLM client), but new subsystems (memory, an LLM
tool-use loop, home automation clients, ...) attach here rather than
spreading global state across modules.

FastAPI gets a single shared instance via `get_siris()`, used as a
dependency, so routes never construct their own.
"""

import time
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from app.audit.service import AuditLogStore
from app.calendar.client import CalendarClient
from app.conversation.service import ConversationStore
from app.core.config import Settings, get_settings
from app.db.session import SessionLocal
from app.docker.client import DockerClient
from app.home_assistant.client import HomeAssistantClient
from app.llm.base import LLMClient
from app.llm.factory import build_llm_client
from app.memory.service import MemoryStore
from app.plex.client import PlexClient
from app.plugins.base import ConfirmationRequired, ToolPermission, ToolRegistry
from app.plugins.builtin.calendar import CalendarUpcomingEventsTool
from app.plugins.builtin.docker import DockerListContainersTool, DockerRestartContainerTool
from app.plugins.builtin.echo import EchoTool
from app.plugins.builtin.home_assistant import (
    HomeAssistantCallServiceTool,
    HomeAssistantGetStateTool,
    HomeAssistantListEntitiesTool,
)
from app.plugins.builtin.memory import RecallTool, RememberTool
from app.plugins.builtin.plex import PlexLibrarySectionsTool, PlexNowPlayingTool
from app.plugins.builtin.system_info import SystemInfoTool
from app.plugins.builtin.unifi import UniFiListClientsTool, UniFiListDevicesTool
from app.plugins.builtin.weather import WeatherCurrentTool
from app.unifi.client import UniFiClient
from app.voice.base import SpeechToTextClient, TextToSpeechClient
from app.voice.factory import build_stt_client, build_tts_client
from app.weather.client import WeatherClient


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
    audit: AuditLogStore = field(default_factory=lambda: AuditLogStore(SessionLocal))

    def __post_init__(self) -> None:
        if self.llm is None:
            self.llm = build_llm_client(self.settings)
        if self.stt is None:
            self.stt = build_stt_client(self.settings)
        if self.tts is None:
            self.tts = build_tts_client(self.settings)
        self._home_assistant_client: HomeAssistantClient | None = None
        self._docker_client: DockerClient | None = None
        self._plex_client: PlexClient | None = None
        self._unifi_client: UniFiClient | None = None
        self._weather_client: WeatherClient | None = None
        self._register_builtin_tools()

    def _register_builtin_tools(self) -> None:
        self.tools.register(EchoTool())
        self.tools.register(RememberTool(self.memory))
        self.tools.register(RecallTool(self.memory))
        self.tools.register(SystemInfoTool(self.settings.system_info_disk_path_list()))

        if self.settings.home_assistant_base_url and self.settings.home_assistant_token:
            self._home_assistant_client = HomeAssistantClient(
                base_url=self.settings.home_assistant_base_url,
                token=self.settings.home_assistant_token,
            )
            self.tools.register(HomeAssistantListEntitiesTool(self._home_assistant_client))
            self.tools.register(HomeAssistantGetStateTool(self._home_assistant_client))
            self.tools.register(HomeAssistantCallServiceTool(self._home_assistant_client))

        if self.settings.docker_base_url:
            self._docker_client = DockerClient(base_url=self.settings.docker_base_url)
            self.tools.register(DockerListContainersTool(self._docker_client))
            self.tools.register(DockerRestartContainerTool(self._docker_client))

        if self.settings.plex_base_url and self.settings.plex_token:
            self._plex_client = PlexClient(base_url=self.settings.plex_base_url, token=self.settings.plex_token)
            self.tools.register(PlexNowPlayingTool(self._plex_client))
            self.tools.register(PlexLibrarySectionsTool(self._plex_client))

        if self.settings.unifi_base_url and self.settings.unifi_api_key:
            self._unifi_client = UniFiClient(
                base_url=self.settings.unifi_base_url,
                api_key=self.settings.unifi_api_key,
                site=self.settings.unifi_site,
                verify_ssl=self.settings.unifi_verify_ssl,
            )
            self.tools.register(UniFiListDevicesTool(self._unifi_client))
            self.tools.register(UniFiListClientsTool(self._unifi_client))

        if self.settings.weather_latitude is not None and self.settings.weather_longitude is not None:
            self._weather_client = WeatherClient(
                base_url=self.settings.weather_base_url,
                latitude=self.settings.weather_latitude,
                longitude=self.settings.weather_longitude,
            )
            self.tools.register(WeatherCurrentTool(self._weather_client))

        if self.settings.caldav_url:
            calendar_client = CalendarClient(
                url=self.settings.caldav_url,
                username=self.settings.caldav_username,
                password=self.settings.caldav_password,
            )
            self.tools.register(CalendarUpcomingEventsTool(calendar_client))

    def restricted_tool_names_to_exclude(self) -> set[str]:
        """RESTRICTED-permission tool names not in the allowlist -- these
        shouldn't even be offered to the model (see `available_tool_specs`)."""
        allowed = self.settings.restricted_tools_allowlist()
        return {
            tool.name
            for tool in self.tools.list()
            if tool.permission is ToolPermission.RESTRICTED and tool.name not in allowed
        }

    def available_tool_specs(self) -> list[dict[str, Any]]:
        """Tool specs to offer the model: everything registered except
        disabled RESTRICTED tools."""
        return self.tools.specs(exclude=self.restricted_tool_names_to_exclude())

    def run_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        conversation_id: str | None = None,
        confirmed: bool = False,
    ) -> Any:
        """Look up and invoke a registered tool by name.

        Raises `ValueError` for an unknown name, `PermissionError` for a
        RESTRICTED tool that isn't enabled, and `ConfirmationRequired` for a
        CONFIRM_REQUIRED tool called with `confirmed=False` (the default) --
        pass `confirmed=True` to actually run it (see `POST
        /siris/chat/confirm`). Every call, whichever of those it hits, is
        recorded to the audit log (app/audit/service.py) along with how
        long it took.
        """
        start = time.monotonic()

        tool = self.tools.get(name)
        if tool is None:
            duration_ms = (time.monotonic() - start) * 1000
            self.audit.record(name, arguments, error="Unknown tool", duration_ms=duration_ms, conversation_id=conversation_id)
            raise ValueError(f"Unknown tool: {name}")

        if tool.permission is ToolPermission.RESTRICTED and name not in self.settings.restricted_tools_allowlist():
            duration_ms = (time.monotonic() - start) * 1000
            error = "Restricted tool not enabled (SIRISAI_RESTRICTED_TOOLS_ENABLED)"
            self.audit.record(name, arguments, error=error, duration_ms=duration_ms, conversation_id=conversation_id)
            raise PermissionError(f"Tool '{name}' is restricted and not enabled.")

        if tool.permission is ToolPermission.CONFIRM_REQUIRED and not confirmed:
            duration_ms = (time.monotonic() - start) * 1000
            self.audit.record(
                name,
                arguments,
                duration_ms=duration_ms,
                conversation_id=conversation_id,
                confirmation_required=True,
            )
            raise ConfirmationRequired(name, arguments)

        try:
            result = tool.run(**arguments)
        except Exception as exc:
            duration_ms = (time.monotonic() - start) * 1000
            self.audit.record(
                name, arguments, error=str(exc), duration_ms=duration_ms, conversation_id=conversation_id
            )
            raise

        duration_ms = (time.monotonic() - start) * 1000
        self.audit.record(name, arguments, result=result, duration_ms=duration_ms, conversation_id=conversation_id)
        return result

    def status(self) -> dict[str, object]:
        """A snapshot of the core service's state, for the status endpoint."""
        return {
            "app_name": self.settings.app_name,
            "environment": self.settings.environment,
            "llm_provider": self.settings.llm_provider,
            "stt_provider": self.settings.stt_provider,
            "tts_provider": self.settings.tts_provider,
            "tools": [tool.name for tool in self.tools.list()],
            "restricted_tools_disabled": sorted(self.restricted_tool_names_to_exclude()),
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
        sync_clients = (
            self._home_assistant_client,
            self._docker_client,
            self._plex_client,
            self._unifi_client,
            self._weather_client,
        )
        for client in sync_clients:
            if client is not None:
                client.close()


@lru_cache
def get_siris() -> SirisAI:
    """Cached accessor for the shared SirisAI instance -- use as a FastAPI dependency."""
    return SirisAI(settings=get_settings())
