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
        env_file=os.environ.get("SIRISAI_ENV_FILE", ".env"), env_prefix="SIRISAI_", extra="ignore"
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

    # Comma-separated tool names allowed to run despite being
    # ToolPermission.RESTRICTED (see app/plugins/base.py). Empty (default)
    # = no RESTRICTED tool can run, whatever the model asks for.
    restricted_tools_enabled: str = ""

    def restricted_tools_allowlist(self) -> set[str]:
        return {name.strip() for name in self.restricted_tools_enabled.split(",") if name.strip()}

    # System info (app/plugins/builtin/system_info.py) -- always registered,
    # no external service, just local resource stats. Comma-separated mount
    # paths to report disk usage for.
    system_info_disk_paths: str = "/"

    def system_info_disk_path_list(self) -> list[str]:
        return [path.strip() for path in self.system_info_disk_paths.split(",") if path.strip()]

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

    # UniFi (app/unifi/, app/plugins/builtin/unifi.py) -- the modern
    # UniFi Network "Integration API" (API-key auth), not the legacy
    # cookie-based controller login. Empty base_url or api_key = unifi_*
    # tools aren't registered. Self-hosted controllers with a self-signed
    # cert need unifi_verify_ssl=false.
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

    # Hermes Agent (app/hermes/, app/plugins/builtin/hermes.py) -- a
    # separate homelab agent running on the same host, with its own
    # memories, skills, and terminal access, used to delegate Docker/
    # system-control tasks it already knows how to do. Talks an
    # OpenAI-style chat completions endpoint, unauthenticated. Empty
    # base_url = the hermes_run_task tool isn't registered.
    hermes_base_url: str = ""
    hermes_model: str = "hermes"

    # Presence-triggered scenes (milestone 42, app/skills/builtin/presence.py):
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
        return [name.strip() for name in self.presence_device_names.split(",") if name.strip()]


@lru_cache
def get_settings() -> Settings:
    """Cached settings accessor -- use as a FastAPI dependency."""
    return Settings()
