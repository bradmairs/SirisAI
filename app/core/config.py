"""Application configuration.

All runtime configuration is centralized here and sourced from environment
variables (or a `.env` file). Nothing else in the app should call
`os.environ` directly -- add new settings here instead.
"""

import os
from functools import lru_cache
from typing import Any

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # env_file is itself overridable via SIRISAI_ENV_FILE (not SIRISAI_*
    # prefixed on purpose -- it configures where to look for the prefixed
    # vars, so it has to be read before that prefix applies) so
    # tests/conftest.py can point it at a file that doesn't exist and get
    # deterministic defaults regardless of a real local `.env` -- otherwise
    # a dev's own `.env` (e.g. real LLM/Home Assistant settings) leaks into
    # every test run just by existing in the repo root.
    model_config = SettingsConfigDict(
        env_file=os.environ.get("SIRISAI_ENV_FILE", ".env"),
        env_prefix="SIRISAI_",
        extra="ignore",
    )

    app_name: str = "SirisAI"
    environment: str = "development"
    log_level: str = "INFO"

    host: str = "0.0.0.0"
    port: int = 8000

    database_url: str = "sqlite:///./data/sirisai.db"

    # Bearer token required on /siris/* routes (Authorization: Bearer <key>).
    # None/empty = auth disabled -- fine for local-only dev, not for anything
    # reachable off localhost. /health is never gated, so infra probes work
    # regardless.
    api_key: str | None = None

    # LLM settings are model-agnostic at the app layer; a specific backend
    # is wired up behind app/llm/base.py.
    llm_provider: str = "none"
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "llama3"

    # Semantic memory recall (app/llm/embeddings.py, app/memory/service.py).
    # "none" (default) keeps MemoryStore.recall() on its original plain
    # substring match -- a paraphrased query that doesn't share literal
    # words with the saved memory just won't find it, a real limitation
    # live-hit in practice. "ollama" turns on real semantic search, reusing
    # `ollama_base_url` above (embeddings and chat completions are almost
    # always served by the same Ollama instance) with a separate model,
    # since embedding models are a different, much smaller model family
    # (e.g. nomic-embed-text, ~274MB) than a chat model -- `ollama pull
    # nomic-embed-text` on the Ollama host to make one available.
    embedding_provider: str = "none"
    embedding_model: str = "nomic-embed-text"

    # Speech (app/voice/base.py). "openai_compatible" points at any local
    # server speaking OpenAI's audio API shape (whisper.cpp server,
    # speaches/faster-whisper-server, openedai-speech, ...).
    stt_provider: str = "none"
    stt_base_url: str = "http://localhost:8081"
    stt_model: str = "whisper-1"

    tts_provider: str = "none"
    tts_base_url: str = "http://localhost:8082"
    tts_model: str = "tts-1"
    tts_voice: str = "default"

    # Home Assistant (app/home_assistant/, app/plugins/builtin/home_assistant.py).
    # Empty base_url or token = the home_assistant_* tools aren't registered
    # at all -- fill both in via .env to enable, no code changes needed.
    home_assistant_base_url: str = ""
    home_assistant_token: str = ""

    # Skip confirmation for home_assistant_call_service calls whose
    # `domain` argument is in this comma-separated allowlist (e.g.
    # "light,switch,fan,media_player") -- everything else still requires
    # confirmation, same as before this existed. Empty (default) = every
    # call still requires confirmation, unchanged. A single "*" means
    # every domain -- a direct user request ("automate this for
    # everything besides a door lock or garage door") that a plain
    # per-domain list would need updating for every time a new device
    # type gets added -- EXCEPT lock and cover (garage doors are a
    # `cover` entity in Home Assistant), which HomeAssistantCallServiceTool
    # always confirms regardless of this setting; see its
    # needs_confirmation() and Tool.needs_confirmation()'s base docstring
    # for the mechanism.
    home_assistant_no_confirm_domains: str = ""

    def home_assistant_no_confirm_domain_list(self) -> list[str]:
        return [
            d.strip()
            for d in self.home_assistant_no_confirm_domains.split(",")
            if d.strip()
        ]

    # Comma-separated tool names allowed to run despite being
    # ToolPermission.RESTRICTED (see app/plugins/base.py). Empty (default)
    # = no RESTRICTED tool can run, whatever the model asks for.
    restricted_tools_enabled: str = ""

    def restricted_tools_allowlist(self) -> set[str]:
        return {
            name.strip()
            for name in self.restricted_tools_enabled.split(",")
            if name.strip()
        }

    # System info (app/plugins/builtin/system_info.py) -- always registered,
    # no external service, just local resource stats. Comma-separated mount
    # paths to report disk usage for.
    system_info_disk_paths: str = "/"

    def system_info_disk_path_list(self) -> list[str]:
        return [
            path.strip()
            for path in self.system_info_disk_paths.split(",")
            if path.strip()
        ]

    # Docker (app/docker/, app/plugins/builtin/docker.py). Talks to the
    # Docker Engine API directly -- no docker-py dependency needed, it's a
    # plain REST API. "unix:///var/run/docker.sock" (default path if you
    # mount the host's socket into this container) or a TCP URL like
    # "tcp://192.168.1.10:2375" for a remote/exposed daemon. Empty =
    # docker_* tools aren't registered.
    docker_base_url: str = ""

    # Plex (app/plex/, app/plugins/builtin/plex.py). Empty base_url or
    # token = plex_* tools aren't registered.
    plex_base_url: str = ""
    plex_token: str = ""

    # UniFi (app/unifi/, app/plugins/builtin/unifi.py) -- a local
    # controller's own "Integration API", API-key auth (see
    # app/unifi/client.py for why not UniFi's cloud account API). Empty
    # base_url or api_key = unifi_* tools aren't registered.
    # unifi_verify_ssl=false for a self-hosted controller's usual
    # self-signed cert.
    unifi_base_url: str = ""
    unifi_api_key: str = ""
    unifi_site: str = "default"
    unifi_verify_ssl: bool = True

    # Weather (app/weather/, app/plugins/builtin/weather.py) via Open-Meteo
    # -- free, no API key. Empty latitude/longitude = weather tool isn't
    # registered.
    weather_base_url: str = "https://api.open-meteo.com"
    weather_latitude: float | None = None
    weather_longitude: float | None = None

    @field_validator("weather_latitude", "weather_longitude", mode="before")
    @classmethod
    def _blank_env_string_means_unset(cls, value: Any) -> Any:
        # An unset .env line (SIRISAI_WEATHER_LATITUDE=) arrives here as ""
        # rather than being omitted entirely, which float | None otherwise
        # rejects -- treat blank the same as not set.
        if isinstance(value, str) and value.strip() == "":
            return None
        return value

    # Calendar (app/calendar/, app/plugins/builtin/calendar.py) via
    # CalDAV -- works with Nextcloud, Radicale, Baikal, iCloud, etc. Empty
    # url = calendar tool isn't registered.
    caldav_url: str = ""
    caldav_username: str = ""
    caldav_password: str = ""

    # Model router (app/llm/router.py). The default ("general") model is
    # still configured via llm_provider/ollama_* above -- this adds an
    # optional second, differently-tuned model ("fast", meant for short/
    # simple prompts) and a policy for routing to it automatically.
    # "off" (default) always uses "general", unchanged from earlier
    # milestones. "prompt_length" routes prompts at or under
    # model_router_fast_max_prompt_chars to "fast", if one is configured.
    model_router_policy: str = "off"
    model_router_fast_max_prompt_chars: int = 120
    fast_llm_provider: str = "none"
    fast_llm_ollama_base_url: str = "http://localhost:11434"
    fast_llm_ollama_model: str = "llama3.2:1b"

    # Proactive notifications (app/notify/client.py,
    # app/plugins/builtin/notify.py, POST /siris/proactive/check) via
    # ntfy.sh -- self-hostable, no account needed, just a topic. Empty
    # topic = the notify tool isn't registered and /siris/proactive/check
    # still runs the health check but never sends anything.
    ntfy_base_url: str = "https://ntfy.sh"
    ntfy_topic: str = ""

    # Actionable push notifications (milestone 43, app/routines/runner.py).
    # Needed only to construct an ntfy action button's target URL -- the
    # externally-reachable base this instance is served at (behind a
    # reverse proxy, a Cloudflare tunnel, etc; SIRISAI_HOST/PORT are for
    # binding, not necessarily what's reachable from outside, e.g. a
    # phone tapping the button). Empty = notifications never carry an
    # action button; everything else about notifications is unaffected.
    public_base_url: str = ""

    # Persona/voice consistency layer (milestone 45, app/cortex/orchestrator.py).
    # A free-text system message applied consistently to every chat turn
    # Cortex handles (process(), confirm(), and the planner loop both
    # drive through) -- how the model communicates, not situational
    # context (app/context/engine.py, milestone 16's use_context, a
    # completely separate concern this is always applied alongside
    # regardless of). Empty (default) = no persona message at all, same
    # raw-model-default behavior as before this milestone -- an
    # opinionated tone is something SirisAI's owner should set
    # deliberately via this setting, not something imposed by default.
    # See .env.example for a starting example.
    persona: str = ""

    # Cost/resource dashboard (milestone 47, app/skills/builtin/server_health.py,
    # GET /siris/metrics/dashboard). LLM token spend (app/llm/metering.py)
    # is always tracked once an LLM backend is configured -- no setting
    # needed. Homelab power draw has no sensor of its own in this
    # codebase, so it reuses the existing Home Assistant integration
    # instead of inventing one: point this at a Home Assistant entity
    # whose state is a wattage reading (e.g. a smart plug/PDU sensor),
    # and server_health_check records it as a metric sample every time it
    # runs. Empty (default) = power draw isn't tracked; requires
    # SIRISAI_HOME_ASSISTANT_BASE_URL/TOKEN to be set too.
    power_sensor_entity_id: str = ""

    # Hermes Agent (app/hermes/, app/plugins/builtin/hermes.py) -- a
    # separate homelab agent running on the same host, with its own
    # memories, skills, and terminal access, used to delegate Docker/
    # system-control tasks it already knows how to do. Talks an
    # OpenAI-style chat completions endpoint. Empty base_url = the
    # hermes_run_task tool isn't registered. hermes_api_key is optional --
    # some Hermes deployments are unauthenticated (the original
    # assumption here), but not all: live-tested against a real instance
    # that turned out to require `Authorization: Bearer <key>`, 401ing
    # every unauthenticated request. Blank = no Authorization header sent
    # at all, same as before this setting existed.
    hermes_base_url: str = ""
    hermes_model: str = "hermes"
    hermes_api_key: str = ""
    # A hardening pass: HermesClient's default (120s) is already generous
    # for Hermes doing its own multi-step tool use/terminal work before
    # responding, but that's still a guess at what any given deployment's
    # heaviest delegated task actually needs -- configurable rather than
    # a silent hard ceiling a genuinely long-running task could hit.
    hermes_timeout_seconds: float = 120.0

    # Automatic memory extraction (a hardening pass, app/cortex/orchestrator.py):
    # after a chat turn produces a real answer, a separate "fast"-role LLM
    # pass (see app/llm/router.py) reviews the exchange and decides, using
    # its own judgment via a small recall/remember tool set (Planner-driven,
    # so it can check existing memory before saving), whether anything is
    # worth keeping long-term -- on by default, since "record things worth
    # remembering without being asked" was the direct ask this exists for.
    # Off entirely turns off the extra LLM call/cost per turn, not just the
    # writes; existing memory tools remain available to the model exactly
    # as they always have either way.
    auto_memory_extraction_enabled: bool = True

    # Timezone SirisAI reports dates/times in (an IANA name, e.g.
    # "Australia/Melbourne"). The model has no clock of its own: the current
    # date/time is injected into every turn's context in this zone, and
    # event times shown to the user are converted to it.
    timezone: str = "UTC"

    # Whether a chat turn may chain several tool calls (plan -> act -> plan
    # ... -> answer, app/planning/planner.py) when the request doesn't say.
    # Default on: found live (2026-09-26) that the single-tool path answered
    # "turn on the light at 40%" correctly only 1 time in 4 (it stopped after
    # the entity lookup), versus 4 in 4 with the planner at similar latency.
    default_use_planner: bool = True

    # Offer the model only the most relevant tools each turn instead of all
    # of them (see app/plugins/selector.py). 110 tools cost ~14,200 prompt
    # tokens per turn versus ~1,050 with none. 0 = always offer everything.
    tool_selection_max: int = 32

    # Background scheduler (app/core/scheduler.py): delivers due reminders and
    # timers and alerts when a tool has been failing. On by default.
    scheduler_enabled: bool = True

    # Read-only web search and page fetch (app/websearch/). Uses DuckDuckGo's
    # HTML endpoint unless SIRISAI_WEB_SEARCH_URL points at a SearXNG instance
    # (its /search?format=json API). Fetching only reaches the public internet.
    web_tools_enabled: bool = True
    web_search_url: str = ""
    scheduler_interval_seconds: float = 20.0
    # "everyone left" -> away scene, "someone's home" -> home scene.
    # presence_device_names is comma-separated UniFi client names/hostnames
    # (e.g. "brads-iphone,jans-iphone") that count as "someone is home"
    # when connected -- matched against the same `name` field
    # unifi_list_clients already reports, not a MAC address (UniFi's own
    # client, app/unifi/client.py, is this codebase's least-verified
    # integration; matching by name keeps this milestone from touching it
    # further). Empty presence_device_names = the presence_check tool
    # isn't registered at all (also needs UniFi itself configured, for
    # unifi_list_clients). Empty presence_home_scene/presence_away_scene =
    # a real transition is still detected and logged, just no Home
    # Assistant scene gets activated for it.
    presence_device_names: str = ""
    presence_home_scene: str = ""
    presence_away_scene: str = ""

    def presence_device_name_list(self) -> list[str]:
        return [
            name.strip()
            for name in self.presence_device_names.split(",")
            if name.strip()
        ]

    # Tautulli (app/tautulli/, app/plugins/builtin/tautulli.py) -- Plex
    # activity/history companion, not a replacement for Plex itself. Both
    # must be set for the tautulli_* tools to be registered.
    tautulli_base_url: str = ""
    tautulli_api_key: str = ""

    # Radarr (app/radarr/, app/plugins/builtin/radarr.py) -- movie
    # collection manager. Both must be set. Generate an API key under
    # Radarr's Settings -> General -> Security.
    radarr_base_url: str = ""
    radarr_api_key: str = ""

    # Sonarr (app/sonarr/, app/plugins/builtin/sonarr.py) -- TV collection
    # manager, same API shape as Radarr (both are Servarr-family apps).
    # Both must be set.
    sonarr_base_url: str = ""
    sonarr_api_key: str = ""

    # Bazarr (app/bazarr/, app/plugins/builtin/bazarr.py) -- subtitle
    # manager for Radarr/Sonarr libraries. Both must be set.
    bazarr_base_url: str = ""
    bazarr_api_key: str = ""

    # Listenarr (app/listenarr/, app/plugins/builtin/listenarr.py) --
    # audiobook/podcast manager. The least-confidently-verified
    # integration in this codebase, more so even than UniFi
    # (app/unifi/client.py's own docstring): built from the shared
    # Servarr-family API convention (X-Api-Key header, /api/v1/*) that
    # Sonarr/Radarr/Lidarr/Readarr/Prowlarr all share, but without a live
    # instance -- or solid reference documentation for Listenarr
    # specifically -- to confirm its exact resource model against.
    # Deliberately scoped to just system/status and queue, the two
    # endpoints near-universal across every Servarr-family app; a
    # library-listing tool needs the real API confirmed first (see
    # app/listenarr/client.py's docstring). Both must be set.
    listenarr_base_url: str = ""
    listenarr_api_key: str = ""

    # Cleanuparr (app/cleanuparr/, app/plugins/builtin/cleanuparr.py) --
    # stalled/failed download cleanup for *arr apps + download clients.
    # Even less confidently verified than Listenarr -- no solid reference
    # documentation for its API was available to build against, so only
    # a status check against a near-universal ASP.NET Core health-check
    # path is implemented; consider onboarding the rest via
    # POST /siris/integrations (milestone 32's OpenAPI discovery) once
    # you can point it at a real running instance's spec, rather than
    # more hand-guessed endpoints here. Both must be set.
    cleanuparr_base_url: str = ""
    cleanuparr_api_key: str = ""

    # Obsidian (app/obsidian/, app/plugins/builtin/obsidian.py) -- not
    # Obsidian itself, which has no API of its own, but the community
    # "Local REST API" plugin
    # (https://github.com/coddingtonbear/obsidian-local-rest-api).
    # Defaults match the plugin's own defaults: HTTPS on 27124 with a
    # self-signed certificate (hence verify_ssl=false to match) unless
    # you've configured the plugin's insecure HTTP mode instead. Both
    # base_url and api_key must be set; the plugin shows the API key in
    # its own settings pane once installed.
    obsidian_base_url: str = "https://127.0.0.1:27124"
    obsidian_api_key: str = ""
    obsidian_verify_ssl: bool = False

    # Frigate (app/frigate/, app/plugins/builtin/frigate.py) -- self-hosted
    # NVR with local object detection; SirisAI's first vision integration.
    # api_key is optional and unused unless set -- a typical self-hosted
    # Frigate instance has no built-in API auth (see app/frigate/client.py's
    # docstring for why), so this integration is gated on base_url alone,
    # unlike every api-key-required one above.
    frigate_base_url: str = ""
    frigate_api_key: str = ""

    # Synology NAS (app/synology/, app/plugins/builtin/synology.py) -- DSM
    # Web API for system status, storage health, disk SMART, and backup tasks.
    # Uses session-based auth (SYNO.API.Auth) with a local DSM user account.
    # All three must be set for the synology_get_status tool to be registered.
    # Create a DSM user with read-only access to the relevant APIs if desired.
    # Verify SSL can be false for self-signed certs on self-hosted DSM.
    synology_base_url: str = ""
    synology_username: str = ""
    synology_password: str = ""
    synology_verify_ssl: bool = True

    # Cron job overview (app/cron/, app/plugins/builtin/cron.py) -- read and
    # toggle crontab entries on other machines on the network (a home Linux
    # server, Raspberry Pis) via plain `ssh` + the remote `crontab` command.
    # Comma-separated "user@host" (or "user@host:port") entries, e.g.
    # "brad@192.168.0.127,root@192.168.0.83". Deliberately no separate
    # key-path setting here -- this shells out to the system `ssh` binary,
    # so it picks up whatever identity file/agent/known_hosts SirisAI's own
    # host user already has configured for reaching these machines (the
    # same access already used to reach them any other way), rather than
    # this codebase reinventing SSH key management. Empty disables the
    # feature entirely (cron_list_jobs/cron_set_job_enabled unregistered,
    # GET /siris/cron_jobs returns an empty list).
    cron_hosts: str = ""

    # Conversation handoff between rooms (app/satellites/service.py,
    # milestone 75). Each satellite process keeps its own local
    # conversation_id in memory (satellite/siris_satellite/client.py) --
    # walking from one satellite's room to another's meant a fresh
    # request with no conversation_id, silently starting a brand-new
    # conversation and losing whatever was just discussed. This is the
    # cutoff: another room's conversation only gets picked up if that
    # room was active within this many seconds -- long enough to walk
    # to another room, short enough that resuming a chat from hours ago
    # would be surprising rather than helpful. Unlike current_room()
    # itself (deliberately no staleness cutoff, per its own docstring --
    # that's "where do I push a reply", where an old answer is still
    # right), a handoff is a stronger claim ("you're still mid-thought"),
    # so it needs its own bound.
    satellite_handoff_window_seconds: float = 120.0


@lru_cache
def get_settings() -> Settings:
    """Cached settings accessor -- use as a FastAPI dependency."""
    return Settings()
