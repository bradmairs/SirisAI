"""Application configuration.

All runtime configuration is centralized here and sourced from environment
variables (or a `.env` file). Nothing else in the app should call
`os.environ` directly -- add new settings here instead.
"""

import os
from functools import lru_cache
from typing import Any, Literal

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
    # How long Ollama keeps a model loaded after a call (4.6): Ollama's own
    # default is 5m, after which the next question waits while the model
    # loads again. "-1" keeps it loaded for good; "" leaves Ollama's default.
    ollama_keep_alive: str = "30m"
    # Local hours (start-end, 24h) when SirisAI keeps the chat model warm
    # with a preload every few minutes, so the first question of the
    # morning doesn't wait for a load. "" = never preload.
    ollama_warm_hours: str = "6-23"
    # Other Ollama machines (comma-separated URLs) tried in order when the one
    # above is unreachable or broken (5xx), e.g. a second AI box both ways
    # round. Used by the general and fast models alike. "" = none.
    ollama_fallback_base_urls: str = ""
    # Whether a reasoning model thinks before answering: "on", "off", or ""
    # for the model's default (gemma4 thinks unless told not to).
    ollama_think: str = ""

    def ollama_fallback_url_list(self) -> list[str]:
        return [u.strip() for u in self.ollama_fallback_base_urls.split(",") if u.strip()]

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
    # Where the embedding model runs, if not on SIRISAI_OLLAMA_BASE_URL (6.0):
    # with a second AI machine, keep embeddings (small, constant) on one and
    # chat on the other, see "Two AI machines" in deploy/README.md.
    embedding_base_url: str = ""

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
    # 1.0 = the backend's natural pace (Kokoro accepts roughly 0.5-2.0).
    tts_speed: float = 1.0
    # Optional ffmpeg `-af` filter chain run over every spoken clip
    # (app/voice/effects.py), e.g. a light "electronic" sheen. Empty = off.
    tts_effect: str = ""

    # Spoken confirmation (POST /siris/voice/converse): when a voice turn
    # needs confirmation, SirisAI asks out loud and a following "yes"/"no"
    # answers it. Domains listed here are never confirmable by voice -- they
    # still need a tap in the app (locks, garage doors/covers, alarms).
    voice_confirmation_enabled: bool = True
    voice_confirmation_blocked_domains: str = "lock,cover,alarm_control_panel"

    def voice_confirmation_blocked_domain_set(self) -> set[str]:
        return {d.strip().lower() for d in self.voice_confirmation_blocked_domains.split(",") if d.strip()}

    # Speaker recognition (app/voice/speaker.py, app/voice/voiceprints.py):
    # enrolled household members are recognised by voice on
    # POST /siris/voice/converse, so memory/goals/"my ..." follow whoever is
    # talking. On whenever the encoder weights exist at this path --
    # `python scripts/fetch_voice_encoder.py models/voice_encoder.npz` (the
    # Docker image does this at build time). A match needs at least this
    # cosine similarity and to be clearly ahead of the next-closest person.
    speaker_id_model_path: str = "models/voice_encoder.npz"
    speaker_id_threshold: float = 0.80

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

    # SirisOS's Home controls (hub /controls): the entity ids to show, in
    # order, comma-separated (light.*, switch.*, cover.*, lock.*). Empty
    # shows every light, garage door and lock (16 at most).
    hub_controls: str = ""

    # Habit suggestions (app/anticipation/habits.py): look back this many
    # days in the Home Assistant logbook for manual actions repeated at the
    # same time; a habit needs at least habit_min_days occurrences (and 60%
    # of eligible days). Checked every habit_check_interval_days; new
    # suggestions arrive as a push with Create/Dismiss buttons (buttons need
    # SIRISAI_PUBLIC_BASE_URL). 0 = never check automatically.
    habit_lookback_days: int = 14
    habit_min_days: int = 5
    habit_check_interval_days: float = 7.0

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

    # Self-healing (app/remediation/runner.py): containers SirisAI may restart
    # on its own when they exit, crash-loop or turn unhealthy (comma-separated
    # names; "*" = every container). Listing one is your standing approval to
    # restart exactly that container without asking. Capped per container and
    # in total per hour; past the cap -- or when a restart doesn't fix it -- you
    # get an urgent push with the last log lines instead. Empty = off.
    remediation_containers: str = ""
    remediation_check_interval_minutes: float = 5.0
    remediation_max_restarts_per_container: int = 2
    remediation_max_actions_per_hour: int = 4
    # How long a restarted container must stay up to count as fixed.
    remediation_settle_seconds: float = 8.0

    def remediation_container_list(self) -> list[str]:
        return [c.strip() for c in self.remediation_containers.split(",") if c.strip()]

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

    @field_validator(
        "weather_latitude", "weather_longitude", "briefing_hour_local", "energy_price_max",
        "brain_dream_hour_local", "brain_weekly_hour_local", "brain_debrief_hour_local",
        "brain_upkeep_hour_local", "reflection_hour_local", "goal_review_hour_local",
        "backup_hour_local", "restore_drill_hour_local", "housekeeping_hour_local", "update_check_hour_local",
        "model_trial_hour_local", "triage_hour_local", "career_capture_hour_local",
        "server_updates_hour_local", "server_disks_hour_local", "container_updates_hour_local", "server_backups_hour_local",
        "internet_report_hour_local", "expiry_check_hour_local", "energy_report_hour_local", "audit_digest_hour_local", "health_weight_goal_kg", "health_check_hour_local", "gmail_hour_local",
        "bills_hour_local", "things_email_hour_local", "travel_hour_local", "reliability_hour_local",
        "self_test_hour_local",
        mode="before",
    )
    @classmethod
    def _blank_env_string_means_unset(cls, value: Any) -> Any:
        # An unset .env line (SIRISAI_WEATHER_LATITUDE=) arrives here as ""
        # rather than being omitted entirely, which float | None (or
        # int | None, for briefing_hour_local) otherwise rejects -- treat
        # blank the same as not set.
        if isinstance(value, str) and value.strip() == "":
            return None
        return value

    # Calendar (app/calendar/, app/plugins/builtin/calendar.py) via
    # CalDAV -- works with Nextcloud, Radicale, Baikal, iCloud, etc. Empty
    # url = calendar tool isn't registered.
    # Email (app/email/client.py) over IMAP/SMTP; an app password works for
    # Gmail/iCloud/Fastmail. imap_host + username + password enable it;
    # smtp_host is only needed to send replies (drafts go over IMAP).
    email_imap_host: str = ""
    email_imap_port: int = 993
    email_username: str = ""
    email_password: str = ""
    email_smtp_host: str = ""
    email_smtp_port: int = 587
    email_from: str = ""
    email_drafts_folder: str = "Drafts"
    email_use_ssl: bool = True

    caldav_url: str = ""
    caldav_username: str = ""
    caldav_password: str = ""
    # Read-only .ics feeds, one per line or comma, each "Label|URL". For
    # Google Calendar use each calendar's "Secret address in iCal format"
    # (Google's CalDAV needs OAuth; the secret address doesn't). Works
    # alongside caldav_url: events from both are merged.
    calendar_ics_urls: str = ""
    # Calendar names to leave out everywhere (comma-separated, any case),
    # e.g. "Birthdays,Holidays in Australia".
    calendar_exclude: str = ""
    # Home Assistant calendars whose id or name contains any of these are
    # skipped by home_assistant_get_calendar_events unless asked for by
    # name: media schedules (Sonarr/Radarr) aren't your day. Their episodes
    # and films stay available through the sonarr_/radarr_calendar tools.
    home_assistant_calendar_exclude: str = "sonarr,radarr,lidarr,readarr"

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
    # The fast role can be the same model with thinking off (5.8): short
    # requests answer in a fraction of the time, long ones still think.
    fast_llm_think: str = "off"

    # Proactive notifications (app/notify/client.py,
    # app/plugins/builtin/notify.py, POST /siris/proactive/check) via
    # ntfy.sh -- self-hostable, no account needed, just a topic. Empty
    # topic = the notify tool isn't registered and /siris/proactive/check
    # still runs the health check but never sends anything.
    ntfy_base_url: str = "https://ntfy.sh"
    ntfy_topic: str = ""
    # Where pushes go (app/notify/routing.py). Quiet hours (local, start-end, 24h): anything
    # below high priority is held for the morning briefing instead. "" = never quiet.
    quiet_hours: str = "22-7"
    # At home with the HUD open, say it on the HUD instead of buzzing the phone.
    notify_speak_at_home: bool = True

    # Actionable push notifications (milestone 43, app/routines/runner.py).
    # Needed only to construct an ntfy action button's target URL -- the
    # externally-reachable base this instance is served at (behind a
    # reverse proxy, a Cloudflare tunnel, etc; SIRISAI_HOST/PORT are for
    # binding, not necessarily what's reachable from outside, e.g. a
    # phone tapping the button). Empty = notifications never carry an
    # action button; everything else about notifications is unaffected.
    public_base_url: str = ""

    # The HTTPS front end's address (e.g. https://192.168.0.100:8443, see
    # caddy/Caddyfile). When set, opening the web pages (/, /hud, /brain)
    # over plain http redirects there, because browsers only allow the
    # microphone on https. The API stays on http. Empty = no redirect.
    https_redirect_url: str = ""

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

    # The car (app/car/service.py), via its Home Assistant integration --
    # each entity optional. climate may be a climate.*, switch.* or button.*.
    car_name: str = "the car"
    car_battery_entity_id: str = ""
    car_range_entity_id: str = ""
    car_charging_entity_id: str = ""
    car_plugged_entity_id: str = ""
    car_location_entity_id: str = ""
    car_lock_entity_id: str = ""
    car_climate_entity_id: str = ""
    car_low_battery_percent: float = 30.0

    # Parcels (app/parcels/service.py): with a 17TRACK API key, statuses are
    # fetched automatically; without one the list is kept by hand.
    seventeentrack_api_key: str = ""
    seventeentrack_base_url: str = "https://api.17track.net"
    parcel_refresh_minutes: float = 120.0

    # The hub contract (app/hub/, /siris/hub/v1) that SirisOS builds on, and
    # its attention feed: how often the synced sources (habit suggestions,
    # jobs waiting for cheap power, queued notices, second-brain reviews) are
    # refreshed in the background. 0 = only when someone asks for the feed.
    attention_sync_minutes: float = 5.0

    # SirisOS (github.com/bradmairs/SirisOS), the home screen: SirisAI calls
    # it for the engineering library it owns (standards, calculators,
    # projects, SirisHydro) and for the state of every app it monitors.
    # sirisos_service_key must match SIRISOS_SERVICE_KEY on the SirisOS side.
    # Empty URL = the sirisos_* tools aren't registered.
    sirisos_url: str = ""
    # SirisOS's address as your phone reaches it (4.6), e.g.
    # https://os.example.net. A push for an approval that needs the PIN gets
    # an "Approve in SirisOS" button opening straight to it. Blank: no button.
    sirisos_public_url: str = ""
    sirisos_service_key: str = ""

    # Energy-aware scheduling (app/energy/service.py): power counts as cheap
    # inside any window in energy_cheap_windows (local "HH:MM-HH:MM", comma-
    # separated, may cross midnight), or when the solar sensor (W or kW) is at
    # or above energy_solar_min_watts, or the price sensor is at or below
    # energy_price_max. Jobs deferred with schedule_for_cheap_energy run at
    # the next such moment.
    energy_cheap_windows: str = ""
    energy_solar_entity_id: str = ""
    energy_solar_min_watts: float = 1500.0
    energy_price_entity_id: str = ""
    energy_price_max: float | None = None
    # Power use for SirisOS's house (5.3, /siris/hub/v1/energy/usage): the
    # whole house's power sensor (W or kW) and its kWh meter, if Home
    # Assistant has them. Every other power and energy sensor is listed too.
    energy_grid_entity_id: str = ""
    energy_meter_entity_id: str = ""
    # The weekly energy report (app/household/energy.py): Mondays at this hour
    # (blank = off), a flat price per kWh when there's no price sensor (0 = no
    # cost), the currency symbol, what counts as "left on overnight", and
    # sensors to leave out (entity ids or names, e.g. the fridge).
    energy_report_hour_local: int | None = 8
    energy_flat_rate: float = 0.0
    energy_currency: str = "$"
    energy_overnight_watts: float = 25.0
    energy_ignore: str = ""
    # Away mode (app/household/away.py): when everyone leaves, "ask" offers the
    # away protocol, "auto" runs it, "off" does nothing; undone on return
    # (doors stay locked).
    away_mode: Literal["off", "ask", "auto"] = "ask"
    away_protocol: str = "away"
    # Camera digest (app/household/cameras.py): what Frigate saw, summarised
    # at these local hours (blank = off), also pushed to the phone.
    camera_digest_hours: str = "12,18,22"
    camera_digest_push: bool = True
    # Health (app/health/): which source wins when two report the same day,
    # a goal weight for the trend (blank = none), how long without new data
    # before the feed says the import has stopped (0 = never), and whether
    # health answers stay off cloud models (a local model, or SirisAI's own
    # words, answers them instead).
    health_source_priority: str = "health_auto_export,apple_export,shortcut,sirisos,manual"
    health_weight_goal_kg: float | None = None
    health_stale_hours: float = 6.0  # 4.17: was 36; only said between 10am and 10pm
    health_readiness_lock_hour: int = 10
    health_max_hr: float | None = None
    spoken_brief_trigger: str = ""
    spoken_brief_player: str = ""
    spoken_brief_tts: str = ""
    spoken_brief_hours: str = "5-11"
    health_private: bool = True
    # Health insights (app/health/insights.py): the getting-sick check's hour
    # (blank = off), Home Assistant sensors for the sleep insights (blank =
    # not used), how long before tomorrow's first event to wake, and the
    # wind-down nudge before bedtime ("ask", "auto" or "off").
    health_check_hour_local: int | None = 10
    health_bedroom_temperature_entity: str = ""
    health_screen_entity: str = ""
    health_morning_buffer_minutes: float = 75.0
    health_wind_down: Literal["off", "ask", "auto"] = "ask"
    health_wind_down_minutes: float = 45.0
    # The weekly audit digest (app/audit/digest.py), Sundays at this hour (blank = off).
    audit_digest_hour_local: int | None = 18

    # Things you own (app/things): warn this many days before a warranty ends.
    warranty_warn_days: int = 30
    # ...and the return window to assume when a receipt doesn't state one (days; 0: only when it says, 5.94).
    return_default_days: int = 0
    # People (app/people): remind this many days before a birthday or anniversary.
    people_remind_days: int = 7
    # House watch (app/household/watch.py), checked every house_watch_seconds
    # (0 = off): fridge/freezer limits (°C) and how long before it counts,
    # and how long a door, garage or gate can stay open.
    house_watch_seconds: float = 120
    fridge_max_c: float = 6.0
    freezer_max_c: float = -12.0
    appliance_warm_minutes: float = 20
    door_open_minutes: float = 15
    # News and podcasts in the morning brief (app/news): RSS/Atom feed URLs
    # (comma-separated), topics to keep (blank: the top of each feed), how
    # many stories, and how long feeds are cached.
    news_feeds: str = ""
    news_topics: str = ""
    podcast_feeds: str = ""
    news_items: int = 5
    news_cache_minutes: float = 30
    # Who's speaking (5.0): what each recognised voice may do, as
    # user=role pairs (adult, child or guest; anyone unlisted is an adult),
    # and whether a voice SirisAI doesn't recognise is treated as a guest
    # (no personal information, nothing that needs confirming).
    voice_roles: str = ""
    voice_unknown_as_guest: bool = True

    # The weekly reliability report (app/upkeep/reliability.py), Sundays at
    # this hour (blank = off).
    reliability_hour_local: int | None = 20

    # Life admin (4.6). The document inbox (app/documents) reminds this many
    # days before anything a letter says is due, and on the day.
    document_remind_days: int = 3
    # Bills and subscriptions found in email (app/bills): checked daily at
    # this hour (blank = off), raised this many days before they're due.
    bills_hour_local: int | None = 8
    bills_warn_days: int = 4
    # Receipts in email become Things (app/things/from_email.py): checked daily at this hour; blank: off.
    things_email_hour_local: int | None = 8
    # Everything with this Gmail label is read as a receipt, whatever its subject or date.
    receipts_label: str = "Receipts"
    # Travel mode (app/travel): trips in the calendar this far ahead, the
    # checklist this many days before, checked at this hour (blank = off).
    # A long event at a place containing travel_home_city isn't a trip.
    travel_lookahead_days: int = 21
    travel_notice_days: int = 3
    travel_hour_local: int | None = 18
    travel_home_city: str = ""

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
    # 20 since 6.0: measured on the live server (2026-11-12, 231 tools, the
    # 71 eval cases) the right tool was offered 100% of the time at 32, 24
    # and 20, 98.6% at 16, 84.5% at 12 and 56% at 10; 20 sends ~1,600 fewer
    # prompt tokens per model call than 32 (~1.5 s a tool turn on the 3060).
    tool_selection_max: int = 20
    # Answer plain home commands ("turn off the kitchen lights", "dim the
    # lounge to 30%") without the model when there's exactly one match and
    # no confirmation is needed (app/cortex/quick.py). Anything else, or
    # anything unclear, goes to the model as before.
    quick_commands: bool = True
    # Also match tools by meaning when an embedding model is configured
    # (SIRISAI_EMBEDDING_PROVIDER=ollama) -- see app/plugins/selector.py.
    tool_selection_semantic: bool = True
    # Run the tool-choice evals (evals/tool_choice.jsonl) against the real
    # model this often, and push a warning if accuracy drops 10+ points.
    # 0 = only on request (POST /siris/evals/run).
    eval_interval_days: float = 7.0

    # The learning loop (app/learning/). Local hour of the nightly reflection
    # (outcomes, eval cases from corrections, memory proposals), and of the
    # weekly goal review (SIRISAI_GOAL_REVIEW_DAY: 0 = Monday). Blank = off.
    reflection_hour_local: int | None = 3
    goal_review_hour_local: int | None = 8
    goal_review_day: int = 0

    # Upkeep (app/upkeep/): what keeps SirisAI running without tending.
    # Backups of the SQLite database (blank hour = off), a restore drill on
    # the 1st of each month, and daily housekeeping.
    backup_hour_local: int | None = 2
    backup_dir: str = ""  # default: backups/ next to the database
    backup_keep: int = 14
    backup_copy_to: str = ""  # a second folder (a NAS mount) to copy each backup to
    restore_drill_hour_local: int | None = 4
    housekeeping_hour_local: int | None = 5
    # The nightly self-test (app/upkeep/self_test.py): asks SirisAI a few
    # questions it should get right (the weather, a light, the day) and checks
    # the answers against the services themselves; a wrong one goes to the
    # attention feed. Blank = off.
    self_test_hour_local: int | None = 4
    audit_keep_days: int = 180
    metrics_keep_days: int = 90
    disk_warn_percent: float = 10.0
    token_budget_daily: int = 0  # 0 = no budget
    # Self-update (deploy/updater.sh applies it): off | notify | auto.
    update_mode: str = "notify"
    update_check_hour_local: int | None = 9
    update_repo: str = "bradmairs/SirisAI"
    update_dir: str = ""  # default: update/ next to the database
    github_api_url: str = "https://api.github.com"
    # Optional for update checks on a public repo; needed for issues and a
    # private repo. A fine-grained token: Contents read, Issues read/write.
    github_token: str = ""
    # GitHub issues for problems SirisAI can't fix itself: blank repo = off;
    # mode "ask" (in the attention feed) or "auto".
    github_issues_repo: str = ""
    github_issues_mode: str = "ask"
    # Eval-gated model upgrades: comma-separated Ollama models to try weekly.
    candidate_models: str = ""
    model_trial_day: int = 6
    model_trial_hour_local: int | None = 1

    # Morning triage (app/triage/): drafts replies to the emails that need
    # you (saved as drafts, never sent unasked) and checks today's calendar
    # for clashes. Blank hour = off. The signature defaults to "Brad".
    triage_hour_local: int | None = 6
    triage_max_drafts: int = 5
    triage_signature: str = ""
    # CPD and chartership evidence capture (app/career/capture.py), evenings.
    career_capture_hour_local: int | None = 18

    # Background scheduler (app/core/scheduler.py): delivers due reminders and
    # timers and alerts when a tool has been failing. On by default.
    scheduler_enabled: bool = True

    # Read-only web search and page fetch (app/websearch/). Uses DuckDuckGo's
    # HTML endpoint unless SIRISAI_WEB_SEARCH_URL points at a SearXNG instance
    # (its /search?format=json API). Fetching only reaches the public internet.
    web_tools_enabled: bool = True
    web_search_url: str = ""
    scheduler_interval_seconds: float = 20.0

    # Proactive checks (app/notify/proactive.py), run automatically by the
    # background scheduler instead of needing an external cron job.
    # 0 disables a given check; each is independent.
    proactive_check_interval_minutes: float = 30.0
    presence_check_interval_minutes: float = 3.0
    # 5.63: "everyone's out" only once every counted phone has been gone this long, without a break. iPhones
    # drop off Wi-Fi when they sleep, so one missed check used to set off the away protocol. Coming home is
    # still immediate. 0 = the old behaviour (the first check with no phone).
    presence_away_after_minutes: float = 10.0
    # Local hour (0-23, in SIRISAI_TIMEZONE) the daily briefing goes out.
    # None disables it.
    briefing_hour_local: int | None = 7
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

    # Protocols (app/protocols/service.py) and guest mode (app/core/guest.py).
    # "night" for protocol conditions and the intruder check: from
    # night_start_hour until night_end_hour, local time. guest_wifi_entity_id
    # is a Home Assistant switch for the guest network (the UniFi integration
    # creates one per WLAN) that the guest protocols turn on and off.
    # Departure heads-up (app/skills/builtin/departure.py): how far ahead a
    # calendar event counts as "coming up" when you leave.
    departure_event_window_hours: float = 3.0
    night_start_hour: int = 22
    night_end_hour: int = 6
    guest_wifi_entity_id: str = ""

    # Intruder response (app/skills/builtin/intruder.py), with Frigate
    # configured: a person detected when intruder_alert_when says so --
    # "away" (nobody home), "night", or "away_or_night" -- raises an urgent
    # alert. intruder_cameras limits it to some cameras (comma-separated;
    # empty = all). intruder_auto_lockdown also runs the lockdown protocol
    # when nobody is home. 0 minutes disables the check.
    intruder_check_interval_minutes: float = 1.0
    intruder_alert_when: str = "away_or_night"
    intruder_cameras: str = ""
    intruder_cooldown_minutes: float = 10.0
    intruder_auto_lockdown: bool = False

    def intruder_camera_list(self) -> list[str]:
        return [c.strip() for c in self.intruder_cameras.split(",") if c.strip()]

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

    # Second brain (app/second_brain/, app/plugins/builtin/second_brain.py)
    # -- a local checkout of Brad's Siris-Second-Brain vault, e.g. cloned
    # into ./data/second-brain so it's already inside the mounted volume
    # (/app/data/second-brain). Setting brain_path registers brain_search/
    # brain_get_note/brain_insights/brain_learn. brain_git_sync pulls before reads (at most
    # every brain_pull_interval_seconds) and commits + pushes after writes;
    # it needs a push-capable git remote, e.g. a deploy key with write
    # access in ./data/ssh. brain_mirror_memory copies `remember` and
    # `remember_entity_fact` facts into the vault too.
    brain_path: str = ""
    brain_git_sync: bool = False
    brain_pull_interval_seconds: float = 60.0
    # Commit the vault's changes together at most this often (0 = one commit per write).
    brain_commit_interval_seconds: float = 900.0
    # Brain watch (app/second_brain/watch.py): alert when a job goes quiet. Hours of silence
    # before an alert: Obsidian sync, unpushed commits, server inventory, secrets backup.
    brain_watch_sync_hours: float = 3.0
    brain_watch_push_hours: float = 6.0
    brain_watch_inventory_hours: float = 50.0
    brain_watch_backup_days: float = 8.0
    brain_mirror_memory: bool = True
    # How many matching notes the context engine adds to each chat turn
    # (0 turns it off). Only reasonably strong matches are used.
    brain_context_notes: int = 3
    # Growth jobs (app/second_brain/jobs.py), all in local time (timezone).
    # An hour of None turns that job off. Meeting notes are opt-in:
    # meeting_calendars names the calendars whose timed events get a note
    # (comma-separated, e.g. "Work"); empty = no meeting notes at all.
    # weekly_day is 0=Monday ... 6=Sunday; gap_questions is how many gaps to
    # ask about each week (0 = off).
    brain_dream_hour_local: int | None = 23
    brain_weekly_day: int = 6
    brain_weekly_hour_local: int | None = 18
    brain_gap_questions: int = 3
    # Notes Siris wrote that it asks Brad to check each week (0 = off).
    brain_review_notes: int = 3
    # Friday work debrief (jobs.work_debrief): three questions about the
    # week's decisions and lessons, pushed at this hour on brain_debrief_day
    # (4 = Friday; an hour of None turns it off), with one question for a
    # CPEng element that has no evidence yet (brain_cpeng_questions).
    brain_debrief_day: int = 4
    brain_debrief_hour_local: int | None = 16
    brain_cpeng_questions: bool = True
    # Nightly upkeep (jobs.upkeep), with the general model (local Ollama):
    # file the inbox, add GitHub activity to project notes, relink. The
    # GitHub step needs a read-only token (contents, pull requests,
    # metadata) for brain_github_owner's repos; empty skips it. On
    # brain_weekly_day the weekly review note is written too.
    brain_upkeep_hour_local: int | None = 2
    brain_github_token: str = ""
    brain_github_owner: str = "bradmairs"
    # Email into Siris (app/gmail/): emails Brad gives the Gmail label below
    # come into Siris' inbox each night, read-only over IMAP. Needs Google
    # 2-Step Verification and an app password (myaccount.google.com/apppasswords);
    # empty = off. Attached pictures are read by the vision model at the upkeep.
    gmail_address: str = ""
    gmail_app_password: str = ""
    gmail_label: str = "Siris"
    gmail_hour_local: int | None = 1
    gmail_max_messages: int = 30

    # Frigate (app/frigate/, app/plugins/builtin/frigate.py) -- self-hosted
    # NVR with local object detection; SirisAI's first vision integration.
    # api_key is optional and unused unless set -- a typical self-hosted
    # Frigate instance has no built-in API auth (see app/frigate/client.py's
    # docstring for why), so this integration is gated on base_url alone,
    # unlike every api-key-required one above.
    frigate_base_url: str = ""
    frigate_api_key: str = ""

    # Image understanding (app/vision/client.py): an Ollama vision model,
    # e.g. "qwen2.5vl:7b", "llava:7b" or "gemma3:4b" (`ollama pull` it first).
    # Enables frigate_look_at_camera / frigate_describe_event (with Frigate
    # configured) and photos attached in chat. Empty = off. vision_base_url
    # defaults to SIRISAI_OLLAMA_BASE_URL.
    vision_model: str = ""
    vision_base_url: str = ""

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
    # A backup that hasn't succeeded within this many hours counts as stale.
    backup_max_age_hours: float = 26.0

    # Security briefing (app/skills/builtin/security.py). cert hosts are
    # "host" or "host:port" (default 443), comma-separated. The scheduler
    # runs the check every security_check_interval_hours (0 = never) and a
    # Trivy image scan every security_scan_interval_days when `trivy` is
    # installed.
    security_cert_hosts: str = ""
    security_cert_warn_days: int = 14
    security_ssh_failed_threshold: int = 25
    security_check_interval_hours: float = 6.0
    security_scan_interval_days: float = 7.0

    def security_cert_host_list(self) -> list[str]:
        return [h.strip() for h in self.security_cert_hosts.split(",") if h.strip()]

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
    # 6.38: check the cron servers this often (minutes) for jobs that didn't run or
    # failed, and keep the overview for the Cron tab. 0: off.
    cron_watch_minutes: float = 30.0

    # SSH to servers on the home network (app/ssh/, app/plugins/builtin/ssh.py):
    # comma-separated "name=user@host[:port]", e.g.
    # "nas=brad@192.168.0.127,pi=root@192.168.0.83". Uses the same ambient SSH
    # identity as the cron overview (no key setting here). Empty = the cron
    # hosts, if any. Look-only commands run straight away; anything else asks
    # first, and with ssh_allow_changes=False never runs. ssh_safe_commands adds
    # command prefixes to treat as look-only, e.g. "pihole status,zpool status".
    ssh_hosts: str = ""
    ssh_allow_changes: bool = True
    ssh_safe_commands: str = ""
    ssh_command_timeout_seconds: float = 30.0
    # 6.38: keep one SSH connection per server open this long (seconds) and send later
    # commands through it (OpenSSH ControlMaster), instead of logging in afresh each
    # time. 0 turns it off. Commands still connect on their own when it isn't there.
    ssh_reuse_seconds: float = 120.0
    # 6.38: after starting on a new version, wait this long (seconds), then check every
    # integration and run the self-test once, alerting on what broke. -1: off.
    release_check_delay_seconds: float = 120.0
    # Reboot/shut down servers (ssh_power): always asks first, never by a
    # spoken yes. ssh_self_server names the server SirisAI itself runs on, so
    # "all" does it last.
    ssh_allow_power: bool = True
    ssh_self_server: str = ""
    # Approvals (app/core/approvals.py): a PIN for high-risk approvals (server
    # power, updates, SSH changes, settings; plus approval_pin_tools), and how
    # long an action held in the attention feed stays approvable.
    approval_pin: str = ""
    approval_pin_tools: str = ""
    approval_expiry_minutes: float = 15.0
    # Wake-on-LAN (app/servers/wake.py). MACs are learned while servers are up;
    # "name=aa:bb:cc:dd:ee:ff,..." overrides. A name that isn't an SSH server is
    # a wake-only computer (a desktop, a laptop); add "@192.168.0.50" so SirisAI
    # can tell when it's up. A relay server sends the packet from inside the
    # LAN (a container can't broadcast to it).
    ssh_wake_macs: str = ""
    ssh_wake_relay: str = ""
    ssh_wake_broadcast: str = ""
    ssh_wake_timeout_seconds: float = 240.0
    # Server watch (app/servers/watch.py): health every N minutes (0 = off),
    # OS updates and disk health once a day (blank hour = off).
    server_watch_minutes: float = 15.0
    server_disk_warn_percent: int = 90
    server_updates_hour_local: int | None = 7
    server_disks_hour_local: int | None = 6
    # Homelab (app/homelab/). Container image updates on the Docker host
    # (daily check, blank hour = off; names to leave alone; how long a new
    # container has to come up healthy before it's rolled back).
    container_updates_hour_local: int | None = 5
    container_update_exclude: str = ""
    container_update_health_seconds: float = 90.0
    container_update_watch_minutes: float = 10.0
    # Backups on the SSH servers: "nas:zfs:tank/data,nas:path:/mnt/backups/restic".
    # Checked daily (blank hour = off), read back on the 1st of each month.
    server_backups: str = ""
    server_backup_max_age_hours: float = 26.0
    server_backups_hour_local: int | None = 8
    # New devices on the network, from UniFi's client list, every N minutes (0 = off).
    network_device_watch_minutes: float = 5.0
    # Internet outage log: a probe every N seconds (0 = off) to these
    # host:port targets (down = none answer), a speed test every N hours
    # (0 = off), and a weekly summary on Mondays (blank hour = off).
    internet_probe_seconds: float = 60.0
    internet_probe_targets: str = "1.1.1.1:443,8.8.8.8:53,9.9.9.9:443"
    internet_speedtest_hours: float = 6.0
    internet_report_hour_local: int | None = 8
    # Certificates and domains about to expire (security_cert_warn_days ahead),
    # daily (blank hour = off); also port 443 on each SSH server.
    security_domains: str = ""
    expiry_check_hour_local: int | None = 9
    expiry_check_servers: bool = True

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
    """Cached settings accessor -- use as a FastAPI dependency. Settings
    changed on the web UI's Settings page win over .env
    (app/core/settings_overrides.py)."""
    from app.core import settings_overrides

    return Settings(**settings_overrides.load())
