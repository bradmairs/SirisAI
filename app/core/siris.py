"""The SirisAI core service.

Everything in the app -- endpoints, tools, the LLM backend, memory -- hangs
off a single `SirisAI` instance. Right now it's thin (settings + a tool
registry + a placeholder LLM client), but new subsystems (memory, an LLM
tool-use loop, home automation clients, ...) attach here rather than
spreading global state across modules.

FastAPI gets a single shared instance via `get_siris()`, used as a
dependency, so routes never construct their own.
"""

import logging
import time
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

import httpx

from app.audit.service import AuditLogStore
from app.capabilities.service import CapabilityGapStore
from app.calendar.client import CalendarClient
from app.context.engine import ContextEngine
from app.conversation.service import ConversationStore
from app.core.config import Settings, get_settings
from app.db.session import SessionLocal
from app.docker.client import DockerClient
from app.goals.service import GoalStore
from app.hermes.client import HermesClient
from app.home_assistant.client import HomeAssistantClient
from app.integrations.openapi import DiscoveredOperation, discover_get_operations
from app.integrations.service import IntegrationStore
from app.llm.base import LLMClient
from app.llm.factory import build_llm_client
from app.llm.router import ModelRouter
from app.memory.entities import EntityMemoryStore
from app.memory.events import EventMemoryStore
from app.memory.service import MemoryStore
from app.metrics.service import MetricsStore
from app.notify.client import NotifyClient
from app.plex.client import PlexClient
from app.plugins.base import ConfirmationRequired, Tool, ToolPermission, ToolRegistry
from app.plugins.builtin.audit import ExplainRecentActionTool
from app.plugins.builtin.capability_gap import ReportCapabilityGapTool
from app.plugins.builtin.calendar import CalendarUpcomingEventsTool
from app.plugins.builtin.docker import DockerListContainersTool, DockerRestartContainerTool
from app.plugins.builtin.echo import EchoTool
from app.plugins.builtin.goals import (
    AddGoalObjectiveTool,
    CompleteGoalObjectiveTool,
    CreateGoalTool,
    ListGoalsTool,
    UpdateGoalStatusTool,
)
from app.plugins.builtin.hermes import HermesRunTaskTool
from app.plugins.builtin.home_assistant import (
    HomeAssistantCallServiceTool,
    HomeAssistantGetStateTool,
    HomeAssistantListEntitiesTool,
)
from app.plugins.builtin.memory import (
    LogEventTool,
    RecallEntityFactsTool,
    RecallEntityRelationshipsTool,
    RecallEventsTool,
    RecallTool,
    RememberEntityFactTool,
    RememberEntityRelationshipTool,
    RememberTool,
)
from app.plugins.builtin.notify import NotifyTool
from app.plugins.builtin.openapi_tool import DynamicOpenAPITool, build_dynamic_tools
from app.plugins.builtin.plex import PlexLibrarySectionsTool, PlexNowPlayingTool
from app.plugins.builtin.routines import CreateRoutineTool, ListRoutinesTool, SetRoutineEnabledTool
from app.plugins.builtin.system_info import SystemInfoTool
from app.plugins.builtin.unifi import UniFiListClientsTool, UniFiListDevicesTool
from app.plugins.builtin.weather import WeatherCurrentTool
from app.routines.service import RoutineStore
from app.skills.base import SkillTool
from app.skills.builtin.self_maintenance import SelfMaintenanceSkill
from app.skills.builtin.server_health import ServerHealthSkill
from app.unifi.client import UniFiClient
from app.voice.base import SpeechToTextClient, TextToSpeechClient
from app.voice.factory import build_stt_client, build_tts_client
from app.weather.client import WeatherClient

# Prefix -> capability category, checked longest-first so "home_assistant_"
# doesn't fall through to a shorter accidental match. New builtin tools
# follow this "<integration>_<action>" naming convention (see
# app/plugins/builtin/), so this stays accurate without a per-tool
# declaration -- add a new integration's prefix here when its client
# lands, same as every other tool wiring in this file.
_CATEGORY_PREFIXES: list[tuple[str, str]] = [
    ("home_assistant_", "home_assistant"),
    ("docker_", "docker"),
    ("plex_", "plex"),
    ("unifi_", "unifi"),
    ("weather_", "weather"),
    ("calendar_", "calendar"),
    ("hermes_", "hermes"),
]
_CATEGORY_NAMES: dict[str, str] = {
    "remember": "memory",
    "recall": "memory",
    "remember_entity_fact": "memory",
    "recall_entity_facts": "memory",
    "remember_entity_relationship": "memory",
    "recall_entity_relationships": "memory",
    "log_event": "memory",
    "recall_events": "memory",
    "notify": "notify",
    "system_info": "system",
    "explain_recent_action": "audit",
    "report_capability_gap": "capabilities",
    "create_goal": "goals",
    "list_goals": "goals",
    "update_goal_status": "goals",
    "add_goal_objective": "goals",
    "complete_goal_objective": "goals",
    "create_routine": "routines",
    "list_routines": "routines",
    "set_routine_enabled": "routines",
}


def _categorize_tool(tool: Tool) -> str:
    if isinstance(tool, SkillTool):
        return "skills"
    if isinstance(tool, DynamicOpenAPITool):
        # All auto-discovered tools (milestone 32) group under one
        # category regardless of which integration discovered them --
        # same reasoning as "skills" being one category rather than one
        # per skill.
        return "discovered"
    for prefix, category in _CATEGORY_PREFIXES:
        if tool.name.startswith(prefix):
            return category
    return _CATEGORY_NAMES.get(tool.name, "misc")


def _describe_policy(tool: Tool, confirmed: bool) -> str:
    """What allowed (or blocked) this call, for the audit log's `policy`
    field (milestone 28) -- derived from the tool's own ToolPermission,
    not something a caller of run_tool() invents itself."""
    if tool.permission is ToolPermission.READ:
        return "READ: no side effects, always runs"
    if tool.permission is ToolPermission.SAFE_ACTION:
        return "SAFE_ACTION: routine and reversible, runs immediately without confirmation"
    if tool.permission is ToolPermission.CONFIRM_REQUIRED:
        return (
            "CONFIRM_REQUIRED: explicitly confirmed before running"
            if confirmed
            else "CONFIRM_REQUIRED: blocked pending confirmation"
        )
    return "RESTRICTED: blocked unless explicitly enabled via SIRISAI_RESTRICTED_TOOLS_ENABLED"


logger = logging.getLogger(__name__)


@dataclass
class SirisAI:
    """The core service. Thin today, the anchor point for everything later."""

    settings: Settings
    llm: LLMClient | None = None
    stt: SpeechToTextClient | None = None
    tts: TextToSpeechClient | None = None
    model_router: ModelRouter | None = None
    tools: ToolRegistry = field(default_factory=ToolRegistry)
    context: ContextEngine = field(default_factory=ContextEngine)
    memory: MemoryStore = field(default_factory=lambda: MemoryStore(SessionLocal))
    entities: EntityMemoryStore = field(default_factory=lambda: EntityMemoryStore(SessionLocal))
    events: EventMemoryStore = field(default_factory=lambda: EventMemoryStore(SessionLocal))
    goals: GoalStore = field(default_factory=lambda: GoalStore(SessionLocal))
    capability_gaps: CapabilityGapStore = field(default_factory=lambda: CapabilityGapStore(SessionLocal))
    routines: RoutineStore = field(default_factory=lambda: RoutineStore(SessionLocal))
    metrics: MetricsStore = field(default_factory=lambda: MetricsStore(SessionLocal))
    integrations: IntegrationStore = field(default_factory=lambda: IntegrationStore(SessionLocal))
    conversation: ConversationStore = field(default_factory=lambda: ConversationStore(SessionLocal))
    audit: AuditLogStore = field(default_factory=lambda: AuditLogStore(SessionLocal))

    def __post_init__(self) -> None:
        if self.llm is None:
            self.llm = build_llm_client(
                self.settings.llm_provider, self.settings.ollama_base_url, self.settings.ollama_model
            )
        if self.model_router is None:
            fast_llm = None
            if self.settings.fast_llm_provider != "none":
                fast_llm = build_llm_client(
                    self.settings.fast_llm_provider,
                    self.settings.fast_llm_ollama_base_url,
                    self.settings.fast_llm_ollama_model,
                )
            self.model_router = ModelRouter(
                fast=fast_llm,
                policy=self.settings.model_router_policy,
                fast_max_prompt_chars=self.settings.model_router_fast_max_prompt_chars,
            )
        if self.stt is None:
            self.stt = build_stt_client(self.settings)
        if self.tts is None:
            self.tts = build_tts_client(self.settings)
        self._home_assistant_client: HomeAssistantClient | None = None
        self._docker_client: DockerClient | None = None
        self._plex_client: PlexClient | None = None
        self._unifi_client: UniFiClient | None = None
        self._weather_client: WeatherClient | None = None
        self._notify_client: NotifyClient | None = None
        self._hermes_client: HermesClient | None = None
        self._openapi_http_clients: list[httpx.Client] = []
        self._register_builtin_tools()

    def _register_builtin_tools(self) -> None:
        self.tools.register(EchoTool())
        self.tools.register(RememberTool(self.memory))
        self.tools.register(RecallTool(self.memory))
        self.tools.register(RememberEntityFactTool(self.entities))
        self.tools.register(RecallEntityFactsTool(self.entities))
        self.tools.register(RememberEntityRelationshipTool(self.entities))
        self.tools.register(RecallEntityRelationshipsTool(self.entities))
        self.tools.register(LogEventTool(self.events))
        self.tools.register(RecallEventsTool(self.events))
        self.tools.register(CreateGoalTool(self.goals))
        self.tools.register(ListGoalsTool(self.goals))
        self.tools.register(UpdateGoalStatusTool(self.goals))
        self.tools.register(AddGoalObjectiveTool(self.goals))
        self.tools.register(CompleteGoalObjectiveTool(self.goals))
        self.tools.register(CreateRoutineTool(self.routines, self.tools))
        self.tools.register(ListRoutinesTool(self.routines))
        self.tools.register(SetRoutineEnabledTool(self.routines))
        self.tools.register(SystemInfoTool(self.settings.system_info_disk_path_list()))
        self.tools.register(ExplainRecentActionTool(self.audit))
        self.tools.register(ReportCapabilityGapTool(self.capability_gaps))

        if self.settings.home_assistant_base_url and self.settings.home_assistant_token:
            self._home_assistant_client = HomeAssistantClient(
                base_url=self.settings.home_assistant_base_url,
                token=self.settings.home_assistant_token,
            )
            self.tools.register(HomeAssistantListEntitiesTool(self._home_assistant_client))
            self.tools.register(HomeAssistantGetStateTool(self._home_assistant_client))
            self.tools.register(HomeAssistantCallServiceTool(self._home_assistant_client))

        # Plex before Docker on purpose: DockerRestartContainerTool's
        # dry_run() optionally reports active Plex sessions a restart
        # would interrupt, so self._plex_client needs to exist (or stay
        # None) before that tool is constructed.
        if self.settings.plex_base_url and self.settings.plex_token:
            self._plex_client = PlexClient(base_url=self.settings.plex_base_url, token=self.settings.plex_token)
            self.tools.register(PlexNowPlayingTool(self._plex_client))
            self.tools.register(PlexLibrarySectionsTool(self._plex_client))

        if self.settings.docker_base_url:
            self._docker_client = DockerClient(base_url=self.settings.docker_base_url)
            self.tools.register(DockerListContainersTool(self._docker_client))
            self.tools.register(DockerRestartContainerTool(self._docker_client, self.entities, self._plex_client))

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

        if self.settings.ntfy_topic:
            self._notify_client = NotifyClient(base_url=self.settings.ntfy_base_url, topic=self.settings.ntfy_topic)
            self.tools.register(NotifyTool(self._notify_client))

        if self.settings.hermes_base_url:
            self._hermes_client = HermesClient(base_url=self.settings.hermes_base_url, model=self.settings.hermes_model)
            self.tools.register(HermesRunTaskTool(self._hermes_client))

        self._register_builtin_skills()
        self._register_discovered_integrations()

    def _register_builtin_skills(self) -> None:
        self.tools.register(SkillTool(ServerHealthSkill(), self))
        self.tools.register(SkillTool(SelfMaintenanceSkill(), self))

    def _register_discovered_integrations(self) -> None:
        """Re-registers every persisted integration's tools (milestone 32)
        -- IntegrationStore only remembers what's needed to regenerate
        them, the ToolRegistry itself is in-memory and empty again on
        every restart. include_secrets=True here is deliberate and
        internal-only: this is the one place auth_header_value is read
        back, to actually build an authenticated httpx client."""
        for integration in self.integrations.list_integrations(include_secrets=True):
            operations = discover_get_operations(integration["spec"])
            self.register_discovered_integration_tools(
                integration["name"],
                operations,
                integration["base_url"],
                integration["auth_header_name"],
                integration.get("auth_header_value"),
            )

    def register_discovered_integration_tools(
        self,
        integration_name: str,
        operations: list[DiscoveredOperation],
        base_url: str,
        auth_header_name: str | None,
        auth_header_value: str | None,
    ) -> list[str]:
        """Builds one shared httpx.Client for `integration_name` (see
        build_dynamic_tools' docstring for why one client, not one per
        operation), registers one DynamicOpenAPITool per operation
        against it -- skipping any whose name is already taken rather
        than crashing -- tracks the client for aclose(), and returns the
        names actually registered. Shared by startup re-registration
        above and POST /siris/integrations so this logic lives in
        exactly one place."""
        if not operations:
            return []
        tools, client = build_dynamic_tools(integration_name, operations, base_url, auth_header_name, auth_header_value)
        self._openapi_http_clients.append(client)

        registered = []
        for tool in tools:
            if self.tools.get(tool.name) is not None:
                logger.warning("Skipping duplicate discovered tool name: %s", tool.name)
                continue
            self.tools.register(tool)
            registered.append(tool.name)
        return registered

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

    def list_tools(self) -> list[dict[str, Any]]:
        """Every registered tool -- including skills, which are just tools
        whose implementation happens to chain other tool calls (see
        app/skills/base.py) -- with the full detail `/siris/status`'s bare
        name list doesn't carry: description, parameters, permission
        level. For developer/UI introspection (milestone 22), not offered
        to the model (that's `available_tool_specs`, which also excludes
        disabled RESTRICTED tools -- this list doesn't filter anything)."""
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
                "permission": tool.permission.value,
            }
            for tool in self.tools.list()
        ]

    def list_skills(self) -> list[dict[str, Any]]:
        """The subset of `list_tools()` that are skills (SkillTool-wrapped)
        rather than a plain single-purpose tool."""
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
                "permission": tool.permission.value,
            }
            for tool in self.tools.list()
            if isinstance(tool, SkillTool)
        ]

    def capabilities(self) -> dict[str, list[str]]:
        """Registered tool names grouped by category -- "what can SirisAI
        do", coarser than the full detail in `list_tools()`. Categories are
        derived from each tool's name (the `docker_`/`plex_`/etc prefix
        convention every builtin tool follows) rather than declared
        per-tool, so this stays accurate without a second place to update
        when a tool is added. A skill is its own "skills" category rather
        than whatever prefix its name happens to have, since composing
        other tools is what makes it a skill."""
        result: dict[str, list[str]] = {}
        for tool in self.tools.list():
            category = _categorize_tool(tool)
            result.setdefault(category, []).append(tool.name)
        return {category: sorted(names) for category, names in sorted(result.items())}

    def system_state(self) -> dict[str, Any]:
        """The world model (milestone 21) in full: every known entity and
        every relationship between them. For introspection/UI -- to ask
        about one entity specifically, recall_entity_relationships (or the
        recall_entity_relationships tool) is the targeted query."""
        return {
            "entities": self.entities.all_entity_names(),
            "relationships": self.entities.all_relationships(),
        }

    def available_model_roles(self) -> list[str]:
        assert self.model_router is not None
        return self.model_router.available_roles()

    def run_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        conversation_id: str | None = None,
        confirmed: bool = False,
        trigger: str | None = None,
        evidence: Any = None,
    ) -> Any:
        """Look up and invoke a registered tool by name.

        Raises `ValueError` for an unknown name, `PermissionError` for a
        RESTRICTED tool that isn't enabled, and `ConfirmationRequired` for a
        CONFIRM_REQUIRED tool called with `confirmed=False` (the default) --
        pass `confirmed=True` to actually run it (see `POST
        /siris/chat/confirm`). Every call, whichever of those it hits, is
        recorded to the audit log (app/audit/service.py) along with how
        long it took.

        Explainable autonomy (milestone 28): `trigger` (what caused this
        call, e.g. "chat: model chose this tool") and `evidence` (JSON-
        serializable supporting data, if any) are the caller's to supply
        and just get carried through to the audit entry -- `policy` isn't
        a parameter, it's derived here from the tool's own permission
        level plus `confirmed`, since that's determined by SirisAI, not by
        whoever's calling. Together they're what
        `explain_recent_action` reads back later.
        """
        start = time.monotonic()

        tool = self.tools.get(name)
        if tool is None:
            duration_ms = (time.monotonic() - start) * 1000
            self.audit.record(
                name,
                arguments,
                error="Unknown tool",
                duration_ms=duration_ms,
                conversation_id=conversation_id,
                trigger=trigger,
                evidence=evidence,
            )
            raise ValueError(f"Unknown tool: {name}")

        policy = _describe_policy(tool, confirmed)

        if tool.permission is ToolPermission.RESTRICTED and name not in self.settings.restricted_tools_allowlist():
            duration_ms = (time.monotonic() - start) * 1000
            error = "Restricted tool not enabled (SIRISAI_RESTRICTED_TOOLS_ENABLED)"
            self.audit.record(
                name,
                arguments,
                error=error,
                duration_ms=duration_ms,
                conversation_id=conversation_id,
                trigger=trigger,
                evidence=evidence,
                policy=policy,
            )
            raise PermissionError(f"Tool '{name}' is restricted and not enabled.")

        if tool.permission is ToolPermission.CONFIRM_REQUIRED and not confirmed:
            duration_ms = (time.monotonic() - start) * 1000
            self.audit.record(
                name,
                arguments,
                duration_ms=duration_ms,
                conversation_id=conversation_id,
                confirmation_required=True,
                trigger=trigger,
                evidence=evidence,
                policy=policy,
            )
            raise ConfirmationRequired(name, arguments)

        try:
            result = tool.run(**arguments)
        except Exception as exc:
            duration_ms = (time.monotonic() - start) * 1000
            self.audit.record(
                name,
                arguments,
                error=str(exc),
                duration_ms=duration_ms,
                conversation_id=conversation_id,
                trigger=trigger,
                evidence=evidence,
                policy=policy,
            )
            raise

        duration_ms = (time.monotonic() - start) * 1000
        self.audit.record(
            name,
            arguments,
            result=result,
            duration_ms=duration_ms,
            conversation_id=conversation_id,
            trigger=trigger,
            evidence=evidence,
            policy=policy,
        )
        return result

    def status(self) -> dict[str, object]:
        """A snapshot of the core service's state, for the status endpoint."""
        return {
            "app_name": self.settings.app_name,
            "environment": self.settings.environment,
            "llm_provider": self.settings.llm_provider,
            "stt_provider": self.settings.stt_provider,
            "tts_provider": self.settings.tts_provider,
            "model_router_policy": self.settings.model_router_policy,
            "model_router_roles": self.available_model_roles(),
            "tools": [tool.name for tool in self.tools.list()],
            "restricted_tools_disabled": sorted(self.restricted_tool_names_to_exclude()),
        }

    async def aclose(self) -> None:
        """Release resources held by the core service (e.g. the LLM/STT/TTS
        clients' connection pools). Call once, at app shutdown."""
        assert self.llm is not None
        assert self.stt is not None
        assert self.tts is not None
        assert self.model_router is not None
        await self.llm.aclose()
        await self.stt.aclose()
        await self.tts.aclose()
        await self.model_router.aclose()
        sync_clients = (
            self._home_assistant_client,
            self._docker_client,
            self._plex_client,
            self._unifi_client,
            self._weather_client,
            self._notify_client,
            self._hermes_client,
        )
        for client in sync_clients:
            if client is not None:
                client.close()
        for openapi_client in self._openapi_http_clients:
            openapi_client.close()


@lru_cache
def get_siris() -> SirisAI:
    """Cached accessor for the shared SirisAI instance -- use as a FastAPI dependency."""
    return SirisAI(settings=get_settings())
