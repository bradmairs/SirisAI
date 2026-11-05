# SirisAI

A Jarvis-style assistant for the homelab: ask it questions, have it query
and control things on the home network, and eventually let it remember what
it learns about you along the way. This repo is the scaffold -- solid
foundations first, capability added in small, shippable steps.

SirisAI is meant to be the orchestration layer, not the model: LLMs, tools,
memory, and (eventually) event handling all sit behind it, model- and
frontend-agnostic. The built-in web UI (milestone 6) is one frontend, not
the only one -- any client that can call `/siris/chat` works.

## Architecture at a glance

Everything hangs off one core service, `SirisAI` (`app/core/siris.py`).
Routes, tools, the LLM client, and (later) memory all attach to that single
object instead of spreading state across modules.

```
app/
  core/
    config.py    # Settings, env-driven (SIRISAI_* vars / .env)
    logging.py   # Logging setup
    siris.py     # The SirisAI core service -- everything hangs off this
  api/routes/     # FastAPI routers (thin; delegate to SirisAI)
  db/             # SQLAlchemy engine/session + declarative Base (SQLite)
  context/
    engine.py     # ContextEngine -- system status + recent events + relevant
                  # memories, prepended to every chat call
  llm/            # Model-agnostic LLM client interface
    base.py       # LLMClient ABC + NullLLMClient placeholder
    ollama.py     # OllamaClient -- the first real backend
    factory.py    # Builds one LLMClient from a provider/base_url/model
    router.py     # ModelRouter -- picks "general" vs "fast" per chat call
  plugins/        # Tool/plugin interface + registry
    base.py       # Tool ABC (+ ToolPermission) + ToolRegistry
    builtin/      # Every tool: echo, memory, home_assistant, system_info,
                  # docker, plex, unifi, weather, calendar
  skills/
    base.py       # Skill ABC + SkillTool (adapts a Skill to the Tool interface)
    builtin/      # server_health.py -- reference skill
  notify/
    client.py     # NotifyClient -- push notifications via ntfy.sh
  memory/
    service.py    # MemoryStore -- free-text long-term memory (SQLite-backed)
    entities.py   # EntityMemoryStore -- facts about named entities
    events.py     # EventMemoryStore -- things that happened, per-entity
  conversation/
    service.py    # ConversationStore -- per-conversation turn history (SQLite-backed)
  audit/
    service.py    # AuditLogStore -- every SirisAI.run_tool() call (SQLite-backed)
  voice/          # Model-agnostic speech interface (STT + TTS)
    base.py       # SpeechToTextClient/TextToSpeechClient ABCs + Null placeholders
    openai_compatible.py  # client for any server speaking OpenAI's audio API shape
    factory.py    # Picks backends from settings.stt_provider/tts_provider
  home_assistant/
    client.py     # Thin sync REST client for Home Assistant's HTTP API
  docker/
    client.py     # Thin sync client for the Docker Engine API
  plex/
    client.py     # Thin sync client for Plex Media Server's API
  unifi/
    client.py     # Thin sync client for UniFi's Network Integration API
  weather/
    client.py     # Thin sync client for Open-Meteo
  calendar/
    client.py     # CalDAV client (wraps the `caldav` library) + CalendarSet (every calendar as one)
    ics.py        # Read-only .ics feeds (Google Calendar's secret address)
  web/static/
    index.html    # Built-in web UI: chat, memory, status. No build step.

satellite/        # Voice satellite -- a SEPARATE deployable, not part of
                   # the Docker image above. Own requirements.txt (adds
                   # sounddevice/openwakeword). See satellite/README.md.
```

**Extension points, by design:**
- **New tool** -- subclass `Tool` in `app/plugins/builtin/` (or your own
  package), register it in `SirisAI._register_builtin_tools`.
- **New LLM backend** -- implement `LLMClient` in `app/llm/`, add a case for
  it in `app/llm/factory.py`. Nothing outside `app/llm/` should know which
  backend is active.
- **New route** -- add a router under `app/api/routes/`, include it in
  `app/main.py`. Keep routes thin; put logic on `SirisAI` or a tool.

## Milestones

Deliberately tiny, each one a working state:

1. **Done** -- FastAPI app in Docker, SQLite wired up, config + logging,
   `SirisAI` core service, `/health` and `/siris/status`.
2. **Done** -- Ollama-backed `LLMClient` (`app/llm/ollama.py`) and a
   `POST /siris/chat` endpoint that calls it. Off by default
   (`SIRISAI_LLM_PROVIDER=none`); set it to `ollama` and point
   `SIRISAI_OLLAMA_BASE_URL` at your Ollama instance to enable.
3. **Done** -- `/siris/chat` offers registered tools to the model
   (`ToolRegistry.specs()`, OpenAI function-calling schema). If the model
   calls one, SirisAI executes it via `SirisAI.run_tool()` and returns the
   result; otherwise the model's text answer is returned. One round-trip
   for now -- feeding the tool result back to the model for a final
   natural-language answer is a natural next step, not required for this
   milestone.
4. **Done** -- Memory subsystem (`app/memory/service.py`, `MemoryStore`,
   backed by a `memories` SQLite table). The model reads/writes it itself
   mid-conversation via the `remember`/`recall` tools
   (`app/plugins/builtin/memory.py`); `GET`/`POST /siris/memory` give
   direct access for inspecting or seeding it outside a chat turn. Recall
   is a plain substring match for now -- semantic/vector search is a
   drop-in replacement behind the same `MemoryStore.recall()` later.
5. **Done** -- Auth: a single shared bearer token (`SIRISAI_API_KEY`),
   checked via `Authorization: Bearer <key>` on every `/siris/*` route
   (`app/core/auth.py`). Off by default (no key configured -> open, fine
   for local-only dev); `/health` is never gated so infra probes still
   work. Deliberately minimal for a single-user service -- no
   users/sessions/OAuth until there's an actual second user.
6. **Done** -- Minimal built-in web UI (`app/web/static/index.html`,
   served at `GET /`): chat, memory search/add, and a status panel. One
   self-contained HTML/CSS/JS file with no build step and no external
   scripts/fonts/CDNs, so it works over a plain homelab connection (e.g.
   behind Tailscale) with no internet access required, and requests it
   makes are same-origin so no CORS setup was needed. It reads the API
   key from a field on the page (stored in the browser's `localStorage`)
   and sends it as the `Authorization` header.
7. **Done** -- Voice primitives: `SpeechToTextClient`/`TextToSpeechClient`
   (`app/voice/base.py`, same model-agnostic pattern as `LLMClient`), with
   an `openai_compatible.py` backend that talks to any local server
   speaking OpenAI's audio API shape -- whisper.cpp's server, speaches /
   faster-whisper-server for STT, openedai-speech (Piper) for TTS. Kept
   external over HTTP rather than embedding model weights, same as Ollama,
   so SirisAI's own container stays lightweight. `POST /siris/voice/transcribe`
   and `POST /siris/voice/speak` expose them directly; nothing composes
   them into a full voice-chat loop server-side yet -- see the next
   milestone.
8. **Done** -- Wired voice into the web UI: a mic button (top-left of the
   chat input) records via the browser's `MediaRecorder`, posts the clip to
   `/siris/voice/transcribe`, and fills the chat input with the transcript
   for the user to review/send (deliberately not auto-sent); a speaker icon
   on every assistant reply calls `/siris/voice/speak` and plays the
   result. Pure client-side composition of the two primitives from
   milestone 7 plus the existing chat endpoint -- no new backend surface.
9. **Done** -- Home Assistant tools (`app/plugins/builtin/home_assistant.py`,
   backed by `app/home_assistant/client.py`, a thin sync wrapper around
   [Home Assistant's REST API](https://developers.home-assistant.io/docs/api/rest/)
   -- sync on purpose, matching `Tool.run()`'s synchronous contract).
   Deliberately generic (domain/service/entity_id, the same shape Home
   Assistant's own service-call model uses) rather than one tool per
   device, so it works for whatever's actually in a given home without
   code changes:
   - `home_assistant_list_entities` -- list entities (optionally filtered
     by domain) and their state, for discovering entity_ids
   - `home_assistant_get_state` -- get one entity's state and attributes
   - `home_assistant_call_service` -- call any service to control a device
     (`domain`, `service`, `entity_id`, optional `service_data`)

   Registered only when both `SIRISAI_HOME_ASSISTANT_BASE_URL` and
   `SIRISAI_HOME_ASSISTANT_TOKEN` are set -- fill those in via `.env` (a
   long-lived access token, from your Home Assistant profile -> Security)
   and restart; no code changes needed. Until then these tools simply
   aren't offered to the model, and won't show up in `/siris/status`.

10. **Done** -- Conversation persistence (`app/conversation/service.py`,
    `ConversationStore`, backed by `conversations`/`messages` SQLite
    tables). `POST /siris/chat` was single-turn and stateless before this;
    now each call's `response` carries a `conversation_id` -- pass it back
    on the next call to continue that conversation, or omit it (or pass an
    unknown one) to start a new one. Prior user/assistant turns are
    replayed to the model as real chat history on every call;
    `GET /siris/conversations/{id}` exposes the full stored history
    (tool exchanges included) for inspection. Required evolving
    `LLMClient.generate()` from a single `prompt: str` to a
    `messages: list[dict]` history -- not scope creep, since
    `OllamaClient` was already POSTing a `messages` array to Ollama's
    `/api/chat`, just always a single-item one. Tool exchanges were
    originally persisted but not replayed to the model as structured
    context on later turns (still just user/assistant text) -- full
    tool-result round-tripping was flagged as future work here; closed in
    the hardening pass noted after milestone 34 below. The built-in web UI
    now carries a conversation across turns within a page session, with a
    "New chat" button to reset it.

11. **Done** -- Tool execution audit log (`app/audit/service.py`,
    `AuditLogStore`, backed by a `tool_executions` SQLite table). Wired
    into `SirisAI.run_tool()` itself rather than the chat route, so
    *every* call goes through it -- including an unknown tool name, and
    including callers other than `/siris/chat` once those exist (e.g.
    skills, milestone 15). Each entry records the tool name, arguments,
    result or error, duration, and (when known) the `conversation_id`
    that triggered it. `GET /siris/activity?limit=50` reads it back, most
    recent first; the web UI's new Activity tab renders it as a feed
    (✓/✗, tool call + args, result or error, timestamp, duration).
    Deliberately built before tools gain any real power -- see the next
    milestone.

12. **Done** -- Tool permission levels. `Tool` now carries a `permission`
    (`app/plugins/base.py`'s `ToolPermission`: `READ` / `SAFE_ACTION` /
    `CONFIRM_REQUIRED` / `RESTRICTED`). `SirisAI.run_tool()` runs
    `READ`/`SAFE_ACTION` immediately; for `CONFIRM_REQUIRED` it raises
    `ConfirmationRequired` instead of running the tool, `POST /siris/chat`
    turns that into a `confirmation_required: {name, arguments}` response
    field, and `POST /siris/chat/confirm` re-submits the same name/
    arguments to actually run it (the web UI shows this as a Confirm/
    Cancel prompt). `RESTRICTED` tools aren't even offered to the model
    (`SirisAI.available_tool_specs()`) unless their name is in
    `SIRISAI_RESTRICTED_TOOLS_ENABLED`; nothing built-in is RESTRICTED yet
    -- the mechanism exists ahead of the first tool that needs it.
    `home_assistant_call_service` is `CONFIRM_REQUIRED` (it's an
    unbounded "call any service" tool -- no way to know in advance
    whether a given call is routine or has real physical consequences).
    Every path -- confirmation requested, restricted-and-blocked,
    executed -- is recorded to the audit log from milestone 11.
13. **Done** -- Expanded the tool library. Same `Tool` pattern as
    `echo`/`remember`/`recall`/`home_assistant_*` throughout -- no new
    mechanism, just more tools, each with its own thin client and each
    conditionally registered from its own `.env` settings (see
    `.env.example`):
    - **System info** (`app/plugins/builtin/system_info.py`) -- CPU,
      memory, disk, temperatures via `psutil`. The one tool here with no
      external service and no credentials -- always registered. Reflects
      this *container's* view unless you give it the host's PID namespace
      and mounts; reports on `/` by default.
    - **Docker** (`app/docker/client.py`) -- talks to the Docker Engine
      API directly (no docker-py dependency), over a Unix socket (mount
      the host's `/var/run/docker.sock` in, see `docker-compose.yml`) or
      TCP. `docker_list_containers` (`READ`), `docker_restart_container`
      (`SAFE_ACTION`). "Update all containers" from the original
      brainstorm isn't implemented -- meaningfully more complex than a
      REST call (pull, stop, remove, recreate) and a better fit for a
      dedicated tool like Watchtower than for this scaffold.
    - **Plex** (`app/plex/client.py`) -- `plex_now_playing`,
      `plex_library_sections`, both `READ`.
    - **UniFi** (`app/unifi/client.py`) -- the modern API-key-based
      Network "Integration API", not the legacy cookie-based controller
      login. `unifi_list_devices`, `unifi_list_clients`, both `READ`.
      Flagged in code as the least-verified integration here -- exact
      paths can shift across controller/UniFi OS versions and this
      couldn't be tested against a live controller; read-only so a path
      mismatch just 404s instead of doing anything destructive.
    - **Weather** (`app/weather/client.py`) -- Open-Meteo, free, no API
      key, just a latitude/longitude. `weather_current`, `READ`.
    - **Calendar** (`app/calendar/client.py`) -- CalDAV via the `caldav`
      library (works with Nextcloud, Radicale, Baikal, iCloud, etc.)
      rather than hand-rolled protocol code -- recurrence expansion and
      PROPFIND/REPORT XML are exactly the kind of thing worth trusting to
      a maintained library, the same reasoning behind using SQLAlchemy and
      httpx elsewhere. `calendar_upcoming_events`, `READ`. Read-only
      `.ics` feeds (`app/calendar/ics.py`, for Google Calendar) are merged
      in alongside it; see "Connecting your calendars".

    None of the five external integrations above could be tested against
    a live service in development (no reachable Docker daemon, Plex
    server, UniFi controller, or CalDAV server) -- each is covered by
    tests against its own client using `httpx.MockTransport` (or mocking
    the `caldav` library for Calendar), verifying request shapes and
    response mapping, same rigor as the Home Assistant integration.
    `system_info` is the exception and is tested for real, since it reads
    local `psutil` data with no external dependency.
14. **Done** -- Memory categories, alongside (not replacing) the existing
    free-text `MemoryStore`:
    - Entity memory (`app/memory/entities.py`, `EntityMemoryStore`, an
      `entity_facts` SQLite table) -- facts about a specific named
      person/service/device/project, looked up by that name rather than
      searched. `remember_entity_fact`/`recall_entity_facts`.
    - Event memory (`app/memory/events.py`, `EventMemoryStore`, an
      `events` table) -- things that happened, optionally tagged to an
      entity. `log_event`/`recall_events`. Lets SirisAI answer "has Plex
      been playing up lately?" from actual history (`recall_events(
      entity_name="Plex")`) instead of just current state.

    Both are always registered (like `remember`/`recall`, no config
    needed) and follow the exact same `Tool` pattern. The model decides
    which of the three memory tools fits what it's told -- their
    descriptions spell out when to prefer one over another.
15. **Done** -- Skills. `app/skills/base.py`'s `Skill` isn't a new registry
    or mechanism -- `SkillTool` adapts it to the existing `Tool` interface
    and registers it in the same `ToolRegistry`, so a skill is offered to
    the model exactly like a tool and gets the same permission gating and
    audit logging for free. A skill's `run(siris, **kwargs)` calls
    `siris.run_tool(...)` for each step, so a `CONFIRM_REQUIRED`/
    `RESTRICTED` step inside a skill is still gated independently -- a
    skill can't launder a dangerous action through composition. Reference
    skill: `server_health_check`
    (`app/skills/builtin/server_health.py`) runs `system_info` and, if
    Docker is configured, `docker_list_containers`, and summarizes
    anything abnormal (>90% CPU/memory/disk, a non-running container).
    `POST /siris/tools/{name}/run` invokes any registered tool or skill
    directly, bypassing the LLM entirely -- the "triggered directly" half
    of this milestone, and generically useful for any tool, not just
    skills (e.g. scripting `docker_restart_container` without a chat
    turn). Outside of a chat turn, so nothing is added to a conversation
    transcript, but every call still hits the audit log.
16. **Done** -- Context engine (`app/context/engine.py`, `ContextEngine`).
    On every `POST /siris/chat` call (unless `use_context: false`),
    prepends a system message with current system status, up to 3 recent
    events, and up to 3 memories matching the prompt -- recomputed fresh
    each turn, so it's never stale, and small on purpose (the roadmap's
    own words: "without every prompt carrying the whole database"). Reads
    state directly (`Tool.run()`, not `SirisAI.run_tool()`) rather than as
    a tool call, so it's best-effort (never blocks a chat turn if
    something in it fails) and deliberately doesn't show up in the audit
    log -- it's background situational awareness, not a decision SirisAI
    made. Not persisted to the conversation transcript either.
17. **Done** -- Event bus: `POST /siris/events` is an inbound webhook so
    external systems (Docker, Home Assistant, UniFi, Plex, calendar, a
    cron job, ...) can push events into SirisAI instead of it only
    discovering things by being asked. `{"source": "docker", "description":
    "container unhealthy", "entity_name": "Plex", "data": {...}}` -- logged
    straight to event memory (14), which now carries `source`/`data`
    alongside `entity_name`/`description`. Deliberately does nothing
    beyond that for now -- deciding whether to ignore, investigate, or
    notify is milestone 19, not this one; the roadmap's own note on that
    milestone already flags full autonomous action as out of scope for
    now. Still immediately useful without any of that: a pushed event is
    picked up automatically by both `recall_events` and the context
    engine (16) on the very next chat call, with no extra wiring --
    verified live by pushing a Docker-style event and watching it appear
    in the system message a subsequent `/siris/chat` call sent to Ollama.
18. **Done** -- Model router (`app/llm/router.py`, `ModelRouter`) -- a
    thin selection layer over the existing `LLMClient` interface, not a
    new client type. The default ("general") model is unchanged, still
    configured via `SIRISAI_LLM_PROVIDER`/`SIRISAI_OLLAMA_*`; this adds
    an optional second, differently-tuned one (`"fast"`, via
    `SIRISAI_FAST_LLM_*`) for short/simple prompts, plus
    `SIRISAI_MODEL_ROUTER_POLICY` (`off` -- always general, the default;
    or `prompt_length` -- auto-route short prompts to `fast` if
    configured). A chat request's `model_role` overrides the policy
    outright (an explicit role that isn't actually configured falls back
    to `general` rather than erroring), and the response's `model_role`
    says which one ran. The pattern generalizes to more roles -- a coding
    model, a cloud model, a "keep private data local" policy that never
    routes to the cloud one -- without a new mechanism, just another
    settings block and a case in `ModelRouter.pick()`; not built further
    than "general" + "fast" here for lack of a second real backend to
    route between yet. `ModelRouter` doesn't own the general client
    itself -- it's passed into `pick()` from `SirisAI.llm` on every call
    -- specifically so it keeps working with whatever `SirisAI.llm`
    currently is.
19. **Done**, in reduced scope -- Proactive notifications. The roadmap
    entry originally on this line described true anomaly detection over
    accumulated event/metric history (e.g. "the backup normally finishes
    by 3am but hasn't today") -- that genuinely needs real history to
    have accumulated first, which a fresh install doesn't have, so it's
    still not built here (metric-side history-based detection landed in
    27; event-side, the exact "backup" example, landed in a later
    hardening pass after milestone 36). What ships instead is the other half of "proactive":
    a notification channel (`app/notify/client.py`, ntfy.sh --
    self-hostable, no account needed, just a topic) and a `notify` tool,
    composed with the *existing* `server_health_check` skill (15, which
    already does real deterministic threshold checks -- CPU/memory/disk,
    non-running containers) behind `POST /siris/proactive/check`: run the
    check, and if it's unhealthy and a notify channel is configured, send
    one notification summarizing the warnings. Deliberately not a
    background scheduler inside this process -- SirisAI doesn't decide on
    its own when to check; something external (a cron job, a systemd
    timer -- see `.env.example` for a crontab line) decides the cadence,
    and the decision to notify is the same deterministic logic
    `server_health_check` already had, not fuzzy LLM judgment. That's the
    "stops well short of autonomous action" boundary this entry always
    intended to draw, just reached by composing what already existed
    rather than inventing historical-trend detection with no history yet
    to detect trends in.
20. **Done** -- Voice satellites. Extends the existing STT/TTS primitives
    (milestones 7-8) beyond the built-in web UI to standalone
    always-listening devices (mic + speaker + wake word) around the house,
    talking to the central SirisAI server. Lives in `satellite/`, a
    **separate deployable** from the FastAPI app/Docker image -- it needs
    hardware-facing dependencies (`sounddevice`, `openwakeword`) that have
    no place in a headless server container, and it's meant to be
    installed directly on the device (a Raspberry Pi with a USB mic/speaker
    is the reference target) rather than run alongside the server. The
    satellite doesn't add anything new server-side: it's just another
    frontend against the same three endpoints the built-in web UI already
    uses (`POST /siris/voice/transcribe` -> `POST /siris/chat` -> `POST
    /siris/voice/speak`), threading `conversation_id` across turns the same
    way. Orchestration (`siris_satellite/satellite.py`: wake word -> record
    -> transcribe -> chat -> speak -> play, resilient to a single cycle
    failing) is built behind an `AudioInput`/`AudioOutput` `Protocol` pair
    specifically so it's fully unit-testable with fakes -- no microphone
    needed. The real hardware implementation (`audio_io.py`: sounddevice
    capture + openWakeWord wake-word detection + `mpg123` playback) is
    intentionally **not** covered by the test suite, same honesty pattern
    as the UniFi client in milestone 13 -- there's no real mic/speaker in
    this sandbox or in CI to verify it against, so it's flagged as the one
    piece to sanity-check by ear on actual hardware. Verified: the full
    10-test orchestration/client suite (`satellite/tests/`, run with its
    own `requirements-test.txt` -- deliberately free of hardware deps so it
    runs anywhere) passes; added a root `pytest.ini` scoping the main app's
    `pytest -q` to `tests/` so the two suites (different dependency sets)
    don't collide; confirmed the main app's 103 tests still pass unchanged.
    See `satellite/README.md` for Raspberry Pi setup, `.env` configuration,
    and wake-word customization.

The roadmap below (21-33) is a second batch, from a further brainstorm --
same rule as the first: nothing here is committed to until its own
milestone lands, and each one gets the same "what was deliberately left
out, and why" treatment on completion. It's a shift in kind, not just more
of the same: milestones 1-20 built an assistant that responds well to one
request at a time. These aim at something that persists and reasons across
requests -- goals it remembers without re-explaining, a model of what's
actually running and how it depends together, plans it can verify rather
than trust, and enough self-inspection to notice its own gaps. A couple of
the source ideas overlapped with each other or with what already exists;
noted inline rather than building the same thing twice under two names.

21. **Done** -- World model / knowledge graph. The brainstorm proposed
    these as two separate ideas ("system state" and "knowledge graph")
    but they're the same concept from two angles -- typed entities with
    typed relationships (`Plex RUNS_ON main-server`,
    `Plex DEPENDS_ON media-storage`) -- so one milestone, not two.
    Extends `EntityMemoryStore` (14) rather than replacing it: a new
    `EntityRelationship` row (`app/db/models.py`) is a directed, free-text-typed
    edge between two entity names, alongside the existing flat
    `EntityFact` rows. `relationships_for(entity_name)` returns edges
    split by direction -- "outgoing" (this entity is the subject: what it
    RUNS_ON/USES/DEPENDS_ON) and "incoming" (this entity is the object:
    what points at it) -- so "what would shutting down media-storage
    affect" is answered by media-storage's *incoming* edges, without a
    real graph database or query language. Two new tools, same shape as
    13's remember/recall pairs: `remember_entity_relationship`
    (SAFE_ACTION) and `recall_entity_relationships` (READ). `relation` is
    free text the model chooses (RUNS_ON, USES, DEPENDS_ON,
    EXPOSED_THROUGH, ...), not an enum -- same reasoning as `EntityFact`'s
    free-text `fact` field, SirisAI stores and queries whatever wording
    the model picks rather than constraining it to a fixed vocabulary up
    front. Deliberately stops there: no transitive traversal ("what does
    X affect two hops away") or cycle handling -- direct edges only for
    now, since nothing built yet (the planner in 24, notification
    correlation in 26) needs more than that. Verified: unit tests for
    `relationships_for`'s outgoing/incoming split and its empty-result
    case for an unknown entity, an end-to-end test through
    `POST /siris/tools/{name}/run`, and a live `uvicorn` run confirming
    the reverse lookup actually surfaces "Plex depends on media-storage"
    when queried from media-storage's side.
22. **Done** -- Developer/introspection API: `GET /siris/tools`,
    `/siris/skills`, `/siris/capabilities`, `/siris/system/state`.
    Deviates from the brainstorm's suggested `/api/*` paths on purpose --
    every existing route lives under `/siris/*` (`/siris/status`,
    `/siris/chat`, `/siris/events`, ...), so a parallel `/api/` namespace
    would just fragment the API surface for no benefit; these four are
    added to the existing `siris.router` instead. `/siris/tools` is
    `SirisAI.list_tools()` -- every registered tool (skills included, per
    `Skill`'s existing "not a new registry" design from 15) with full
    detail (description, parameters, permission) that `/siris/status`'s
    bare name list doesn't carry, and unlike `available_tool_specs()` it
    doesn't hide disabled RESTRICTED tools, since the point is showing
    what's installed, not what the model can currently call.
    `/siris/skills` is the `SkillTool` subset of the same list.
    `/siris/capabilities` groups tool names by category (`{"docker": [...],
    "weather": [...], "memory": [...], "skills": [...]}`) -- categories
    are derived from each builtin tool's existing `<integration>_<action>`
    naming convention rather than declared per-tool, so adding a new
    integration's tools doesn't require also remembering to register them
    in a second place. `/siris/system/state` exposes the whole world model
    (21) at once: `EntityMemoryStore` gained `all_entity_names()`, the
    union of entities with a fact *and* entities that only ever appear as
    a relationship's subject/object (e.g. "main-server" from a `RUNS_ON`
    edge, with no fact of its own) -- otherwise an entity that's purely a
    dependency target would be invisible to this endpoint despite being
    very much part of the graph. Verified: unit and endpoint tests for all
    four routes (including that every tool lands in exactly one
    capability category, and that a fact-less relationship-only entity
    still appears in system state), plus a live `uvicorn` run exercising
    all four against a real request. Full suite: 116 passed.
23. **Done** -- Goals. A goal (e.g. "reduce homelab power consumption")
    persists across conversations with a status and a list of objectives,
    so it doesn't need re-explaining every session. New `GoalStore`
    (`app/goals/service.py`), same SQLite-backed "own short-lived session
    per call" pattern as `MemoryStore`/`EntityMemoryStore`, backed by two
    tables (`Goal`, `GoalObjective`) rather than one -- a goal has a real
    lifecycle (objectives added over time, individually completable,
    overall status changed independently) that a single free-text row
    like `EntityFact` can't represent. Five tools rather than a
    remember/recall pair, for the same reason: `create_goal`,
    `list_goals`, `update_goal_status`, `add_goal_objective`,
    `complete_goal_objective` -- each single-purpose, the roadmap entry's
    proposed single `manage_goals` tool would've meant one tool with a
    pile of mutually-exclusive optional arguments, which the rest of
    `app/plugins/builtin/` deliberately avoids. `status` is free text
    (recommended: active/completed/abandoned), same reasoning as
    `EntityFact.fact` and `EntityRelationship.relation` -- SirisAI stores
    and filters on whatever the model picks rather than enforcing a fixed
    vocabulary. `GET /siris/goals` added alongside, same relationship to
    `list_goals` as `GET /siris/events` already has to `recall_events`.
    Deliberately just persistent state at this stage -- completing every
    objective does not auto-complete the goal, and nothing here works a
    goal autonomously in the background; that's the planner (24).
    Verified: unit tests (creation with/without objectives, status
    filtering and ordering, unknown-id handling, completing an objective
    doesn't touch the goal's own status), endpoint tests, and a live
    `uvicorn` run through the full lifecycle (create with three
    objectives, complete one, list filtered by status). Full suite: 126
    passed.
24. **Done** -- Task planner (`app/planning/planner.py`, `Planner`).
    Before this, a chat turn was: model picks zero or one tool call,
    SirisAI runs it, done -- `Planner.run()` instead loops
    generate -> execute step -> feed result back -> generate again, up to
    `max_steps` (default 5), so "figure out why Plex is slow" can chain
    check-container-health -> check-CPU/RAM -> check-disk-I/O without the
    conversation needing a manual turn per step. Opt-in via
    `ChatRequest.use_planner` (default `false`) -- the single biggest
    architectural change in this batch, so it was built as an additional
    path through `POST /siris/chat` rather than a replacement, and every
    existing test (and caller) sees no behavior change unless it asks for
    one. `plan` (the ordered list of every call+result made) replaces
    `tool_call` in the response when it's used.

    On the verifier: this does NOT implement a separate deterministic
    verification pass that can tell "this 200 OK doesn't actually mean
    the service is healthy" independent of the model's own judgment --
    that needs per-tool success criteria this milestone doesn't define,
    and would have been overclaiming a rigor that isn't really there. (A
    later hardening pass, after milestone 37, added exactly that as an
    opt-in per-tool mechanism -- `Tool.verify()` -- without changing what's
    described here: the model's own re-assessment is still what drives
    the planner loop; verification is a separate, always-on, independent
    check recorded to the audit log alongside it.) What it does implement
    here is real, just more modest: every tool result
    is fed back to the model before it decides on a next step, so
    "verification" is the model re-assessing actual result *content*
    against the original request each iteration (not just whether the
    call raised) -- if the evidence doesn't add up, it calls another
    tool; if it does, it answers. A CONFIRM_REQUIRED tool mid-plan still
    stops the loop and surfaces `confirmation_required` with whatever
    steps already ran (see `PlanResult.pending_confirmation`) -- resuming
    the rest of the plan after confirming was originally out of scope here
    (`POST /siris/chat/confirm` ran just that one step); a later hardening
    pass (see after milestone 35 below) added that as an opt-in on
    `POST /siris/chat/confirm` itself. Every step still
    goes through `SirisAI.run_tool()` -- full permission gating and audit
    logging apply per step, nothing about planning bypasses them.

    Verified: unit tests against `Planner.run()` directly with a fake LLM
    client and a fake `siris` stub (multi-step chaining, stopping on
    confirmation-required/permission-error/other-failure with prior steps
    preserved, and exhausting `max_steps` without a final answer),
    endpoint tests confirming `use_planner=false` leaves existing
    single-tool-call behavior untouched, and a live `uvicorn` run against
    a real mock Ollama HTTP backend (not just a fake `LLMClient`) that
    actually returns two tool calls before a final answer -- confirmed
    both steps landed in the audit log individually via
    `GET /siris/activity`. One thing self-caught before it shipped: the
    first pass of these tests was `async def`, which pytest silently
    skips without `pytest-asyncio` installed (not a dependency here,
    same pitfall milestone 18 hit) -- converted to synchronous tests
    wrapping `asyncio.run()` instead, and confirmed they actually execute
    (six previously-silent tests went from "skipped" to "passed" in the
    same run). Full suite: 134 passed.
25. **Done** -- Simulation / dry-run mode. `Tool` gained an optional
    `dry_run(**kwargs)` alongside `run()` (`app/plugins/base.py`) --
    default implementation just raises `NotImplementedError`, so every
    existing tool is unaffected unless it deliberately opts in; most
    tools have nothing worth simulating anyway (a READ tool has no
    effect to preview). `POST /siris/tools/{name}/run` gained a
    `dry_run: true` request field that calls `dry_run()` instead of
    `run()` -- bypassing `SirisAI.run_tool()` entirely, since a dry run
    has no effect to permission-gate or audit; a tool that doesn't
    support it returns 501.

    One real implementation: `DockerRestartContainerTool.dry_run()`
    (`app/plugins/builtin/docker.py`) reports the container's current
    state (restarting a stopped container isn't the same as a running
    one) plus, genuinely useful rather than a toy example, ties two
    earlier milestones together instead of hand-coding a Plex-specific
    special case: it queries the world model (21) for what depends on
    the container being restarted (`EntityMemoryStore.relationships_for()`'s
    incoming edges -- whatever's been recorded via
    remember_entity_relationship), and separately, if the container's
    name looks like Plex and a Plex client is configured, reports the
    live count of active streams that restart would interrupt -- the
    concrete, actionable number the brainstorm's own example asked for,
    on top of the general dependency list. Required reordering Plex's
    client setup before Docker's in `SirisAI._register_builtin_tools`
    (`DockerRestartContainerTool` now takes `entities` and an optional
    `plex` client) so the Plex client exists, or is cleanly `None`, by
    construction time. An unreachable Plex server doesn't fail the dry
    run, it just omits that detail -- best-effort preview, not another
    thing that can block on a flaky integration.

    Sequenced before 24 fully matures on purpose (built right after it):
    a planner chaining multiple SAFE_ACTION/CONFIRM_REQUIRED tool calls
    is exactly where a dry run matters most. Chat-level "simulate this
    whole plan before running it" integration is NOT built here, though
    -- this milestone is the `Tool.dry_run()` interface, one real
    implementation, and direct-invocation support; wiring dry-run into
    `POST /siris/chat`/the planner itself is a natural follow-up, not
    something the roadmap entry for this milestone actually promised.
    Verified: unit tests (default raises, current-state reporting for a
    found/not-found container, world-model dependents, Plex session
    count shown only for a Plex-shaped container name, and that an
    unreachable Plex client doesn't fail the dry run), endpoint tests
    (501 for an unsupported tool, a supported tool's preview leaving
    nothing in the audit log, 404 for an unknown tool), and a live
    `uvicorn` run against real mock Docker and Plex HTTP backends --
    confirmed the report correctly showed a real dependent entity and a
    real live session count, and confirmed the audit log stayed empty
    for the dry run itself. Full suite: 144 passed.
26. **Done** -- Notification intelligence
    (`app/notify/manager.py`, `NotificationManager`). Replaces the flat
    "unhealthy -> send one ntfy message joining every warning" logic in
    `POST /siris/proactive/check` (19): each `server_health_check` (15)
    warning is scored -- severity, category, confidence, an `action`
    (urgent/notify/log_only/ignore, though only the top two are reachable
    today, see below) -- and only "notify"/"urgent" items actually get
    pushed; `notifications` in the response shows every item's
    classification regardless, so what wasn't sent is still visible, not
    silently dropped.

    Also correlates related warnings using the world model (21) instead
    of surfacing them as separate, seemingly unrelated lines: if a
    container warning's name has a recorded relationship
    (`remember_entity_relationship`) pointing at something a disk-usage
    warning is also about, they merge into one message ("Container 'plex'
    is exited -- likely related to: Disk usage on media-storage is high")
    with the more severe of the two winning the merged action.
    Deliberately narrow about how: correlation is exact-string matching
    against the container name and disk path `server_health_check`'s own
    messages already contain (a regex against its two known message
    shapes), not a general entity-extraction system -- a relationship
    recorded as "Plex" won't match a warning naming "plex". Confidence is
    always 1.0 and nothing lands on "ignore": every warning
    `server_health_check` produces already cleared its own hard threshold
    (>90%, not running), so there's no genuine uncertainty to model yet --
    a future source with real uncertainty (an LLM's own read of a
    situation, a learned baseline in 27) is what would actually use the
    fuller action range this milestone's data model already supports.

    Verified: unit tests against `NotificationManager` directly
    (classification of each warning shape, correlation with/without a
    matching relationship, correlation declining to match an unrelated
    disk warning, merged severity/action picking the more severe side),
    endpoint tests (existing `/siris/proactive/check` behavior unchanged
    for non-correlating warnings, a new correlation case, `notifications`
    populated even when nothing was sent), and a live `uvicorn` run
    against a real mock Docker backend (a container in `exited` state)
    and a real mock ntfy server -- confirmed the exited-container warning
    was classified critical/urgent and the mock ntfy server actually
    received the push with the correct "SirisAI: urgent" title. Full
    suite: 154 passed.
27. **Done** -- Personalized thresholds (`app/metrics/service.py`,
    `MetricsStore`). `server_health_check` (15) hard-coded CPU/memory/disk
    >90%; each metric now also gets flagged at `mean + 2*stdev` over its
    own recent history once enough samples exist (`_MIN_SAMPLES_FOR_BASELINE
    = 10`) -- so "67% CPU" can be unusual for *this* server without an
    arbitrary global cutoff -- but the fixed 90% stays a hard ceiling
    either way (`min(90, mean + 2*stdev)`): a server that legitimately
    runs hot all the time doesn't get its real problems suppressed just
    because that's "normal" for it. Same honest scope-cut as milestone
    19: a fresh install has no history, so `MetricsStore.baseline()`
    returns `None` below the sample floor and `server_health_check`
    falls back to the plain fixed threshold until then -- no pretending
    day-one readings mean anything. Samples only accumulate when
    something actually calls `server_health_check` (a chat turn, cron
    hitting `/siris/proactive/check`) -- no in-process scheduler taking
    readings on its own, same milestone-19 boundary as 26.

    One integration detail worth calling out: a baseline-triggered
    warning keeps the *exact* `"{label} is high: {value}%"`
    prefix/shape the fixed-threshold path always used, with `(baseline
    {mean}% ± {stdev}%, {n} samples)` appended after rather than
    replacing it -- milestone 26's `NotificationManager` correlates on
    that exact shape via regex, so a personalized-threshold warning
    correlates with a recorded world-model relationship exactly the same
    way a fixed-threshold one always did, not a parallel message format
    that would silently stop correlating.

    Verified: unit tests against `MetricsStore` directly (ordering, limit,
    the below-floor `None` case, mean/stdev computation), `ServerHealthSkill`
    integration tests (a fresh metric only uses the fixed threshold; an
    established stable baseline flags a value the fixed threshold would
    have missed; the fixed threshold still wins as a ceiling against a
    server that runs hot; a baseline-triggered warning still correlates
    through `NotificationManager` end to end), and a live `uvicorn` run
    against the real host: seeded 10 real idle-CPU readings, then spiked
    real CPU load (`yes` across all cores) and confirmed the resulting
    warning carried real baseline numbers -- and, more tellingly, a
    completely stable ~4.5% memory reading got flagged the moment it
    ticked up to 4.6%, nowhere near the old 90% threshold, which is
    exactly the kind of catch a fixed global cutoff can't make. Full
    suite: 162 passed.
28. **Done** -- Explainable autonomy. `ToolExecution` (11) gained three
    nullable columns -- `trigger` (what caused the call, e.g. "chat: model
    chose this tool", "planner: step 2 of an in-progress plan", "proactive
    check: automated schedule", "direct API request: a human explicitly
    asked for exactly this tool"), `evidence` (JSON-encoded supporting
    data, when there was any -- a planner's prior step results, or the
    classified warnings that justified a proactive notification), and
    `policy` (what allowed it to run without asking first) -- extending
    the existing audit entry rather than a parallel log, per the roadmap's
    own instruction. `policy` isn't a caller-supplied field: `SirisAI.run_tool()`
    derives it itself from the tool's own `ToolPermission` plus whether it
    was confirmed (`_describe_policy()`), since that's determined by
    SirisAI, not by whoever's calling -- keeps it consistent regardless of
    which of the four call sites (`/siris/chat`, `/siris/chat/confirm`,
    the planner, `POST /siris/tools/{name}/run`) triggered it. `trigger`/
    `evidence` are each call site's to supply, and now are: chat threads
    through which of the two confirm/non-confirm flows it came from,
    the planner records each step's number and everything the prior
    steps returned as evidence for the next one, and
    `POST /siris/proactive/check`'s notify call carries the classified
    `NotificationManager` (26) items that justified sending as evidence
    -- while direct tool invocation is explicitly labeled a *non*-autonomous
    trigger, matching the milestone's own framing that a human asking for
    exactly this tool by name isn't the kind of action this needed to
    explain.

    The actual "answer 'why did you restart Plex last night'" capability
    is a new `explain_recent_action` tool (`app/plugins/builtin/audit.py`,
    always registered): pass a `tool_name` (and an optional
    `argument_contains` substring filter, e.g. "plex") and it returns the
    matching recent audit entries -- trigger, evidence, and policy
    included -- via a new `AuditLogStore.find_recent()`. Without this, the
    richer audit data would sit in a table nobody queries; with it, the
    model can actually answer the question conversationally from stored
    fact instead of guessing from whatever's still in context.

    Verified: unit tests for each policy string (READ/SAFE_ACTION/
    CONFIRM_REQUIRED before and after confirmation), trigger/evidence
    round-tripping through `run_tool()` directly, chat- and
    direct-invocation-driven calls each recording their own distinct
    trigger, `explain_recent_action`'s argument-substring filtering, and
    the proactive-check notify call's evidence; plus a live `uvicorn` run
    against real mock Docker and Ollama backends driving the actual
    "figure out why Plex is down and fix it" scenario through the planner
    (24) end to end, then asking `explain_recent_action` afterward and
    confirming it returned the real trigger ("planner: step 2..."), real
    evidence (the exited-container reading from step 1), and real policy
    for that specific restart. Full suite: 172 passed.
29. **Done** -- Routine engine. Natural-language routines ("every night,
    check my server and tell me tomorrow if anything went wrong") stored
    as structured data -- name, trigger description, an ordered list of
    existing tool names, an optional output note -- via `RoutineStore`
    (`app/routines/service.py`), rather than living only as a prompt.
    The open design question this entry flagged (whether running a
    routine on schedule means teaching an external trigger to enumerate
    stored routines, keeping milestone 19's no-in-process-scheduler
    boundary, or adding a real scheduler and reversing it) was put to the
    user rather than picked silently -- answer: keep the boundary.
    `POST /siris/routines/run_due` is the endpoint an external cron/
    systemd timer calls (same relationship `POST /siris/proactive/check`
    already has to `server_health_check`); SirisAI decides which stored
    routines are currently due (`RoutineStore.due_routines()`,
    interval-based -- "at least every N minutes since it last ran", not a
    full cron expression, so "nightly" works but "nightly at 2am
    specifically" doesn't) and `RoutineRunner`
    (`app/routines/runner.py`) runs each due routine's actions in order
    through the normal `SirisAI.run_tool()` path -- full permission
    gating and audit logging apply per action, and each action's own
    `trigger` records which routine caused it (composes directly with 28:
    `explain_recent_action` on a routine-triggered call shows exactly
    that). One broken action doesn't stop the rest of a routine or block
    it from being marked run -- a nightly check with one failing step
    still runs its other steps, and isn't retried every cron tick until
    fixed. Three tools (`create_routine`, `list_routines`,
    `set_routine_enabled`) rather than a single do-everything one, same
    convention as goals (23) -- `create_routine` validates every action
    name against the live tool registry up front, so a routine can't be
    created referencing a tool that doesn't exist. `GET /siris/routines`
    added alongside, same relationship to `list_routines` as `GET
    /siris/events`/`/siris/goals` already have to their own tools.
    A real gotcha caught before it shipped: SQLite silently drops tzinfo
    on a stored `datetime.now(UTC)` when read back, so comparing a
    freshly tz-aware "now" against a routine's stored `last_run_at`
    raises a naive/aware `TypeError` -- fixed by normalizing to naive UTC
    for the comparison and documenting why, rather than papering over it
    with a try/except.

    Verified: unit tests against `RoutineStore` (creation, enabled
    filtering, the due/not-due/became-due-after-interval-elapsed cases,
    a disabled routine never being due), the `create_routine` tool
    rejecting an unknown action name or a non-positive interval,
    `RoutineRunner` (actions run in order, a failed action doesn't stop
    the rest, still marked run), and endpoint tests including a full
    create -> run_due -> not-immediately-due-again round trip; plus a
    live `uvicorn` run creating a real routine, running it via
    `POST /siris/routines/run_due`, confirming it wasn't immediately due
    again, and confirming `explain_recent_action` showed the exact
    `"routine: 'overnight_server_watch' (nightly)"` trigger for the
    resulting `server_health_check` call. Full suite: 185 passed.
30. **Done** -- Reflection / maintenance cycle. A self-inspection skill,
    `self_maintenance_check` (`app/skills/builtin/self_maintenance.py`,
    same `Skill` pattern as 15's `server_health_check`), that reports on
    SirisAI itself rather than the server it runs on. Merged two of the
    roadmap's listed signals rather than building two mechanisms that say
    the same thing: this codebase has no separate notion of "integration
    health" apart from tool execution -- a repeatedly-failing UniFi
    client or an unreachable Docker socket already shows up as a flaky
    tool in the audit log, so "tools that fail often" and "integrations
    erroring" are one stat, `AuditLogStore.failure_counts_by_tool()`.
    "Unused memories" needed real usage tracking that didn't exist yet:
    `MemoryEntry` gained `recall_count`/`last_recalled_at`, bumped every
    time `MemoryStore.recall()` actually returns an entry (not by
    `list_all()`, the read path this skill itself uses to inspect
    everything -- that's not the model finding something relevant, so it
    shouldn't count as a use). A memory younger than 7 days never counts
    as unused -- it hasn't had a real chance to come up yet, an arbitrary
    but documented floor, same as this project's other threshold
    constants. "Duplicate memories" is exact-content grouping -- no
    fuzzy/semantic matching, consistent with `recall()` itself still
    being substring-based.

    The standout capability, called out specifically in the roadmap:
    `AuditLogStore.repeated_planner_sequences()` mines the audit log for
    tool-call sequences the planner (24) has repeated across at least 3
    distinct plans, and turns each one into a concrete suggestion
    ("'system_info -> echo' has run as a plan 3 times -- consider turning
    it into a reusable skill") instead of a vague one. There's no
    separate "plan id" stored anywhere, so a "sequence" is inferred from
    the trigger field's own step counter (28) resetting to 1, or the
    conversation changing -- documented as a heuristic over existing data
    rather than a new stored concept. Every finding in this skill is
    data-backed this way, not an LLM's impression of what seems off.

    Verified: unit tests for memory usage tracking (recall bumps it,
    `list_all()` doesn't, only matching entries count),
    `failure_counts_by_tool` (only tools with real failures, sorted worst
    first), `repeated_planner_sequences` (detected across distinct
    conversations, below-threshold not reported, single-step "sequences"
    excluded, two separate plans in the same conversation correctly kept
    apart, a non-planner call in between doesn't merge sequences across
    it), and the skill's own report shape; plus a live `uvicorn` run that
    seeded two real duplicate memories with one recalled, two genuine
    tool failures (`update_goal_status` on unknown ids), and three
    identical two-step plans across three separate conversations driven
    through a real mock Ollama backend -- confirmed
    `self_maintenance_check` correctly reported the duplicate pair, the
    real failure count, the repeated sequence at exactly 3 occurrences,
    and the resulting skill suggestion, with `unused_memories` correctly
    empty since nothing was old enough yet. Full suite: 199 passed.
31. **Done** -- Capability gap detection. A key design realization drove
    the implementation: detection doesn't need a separate mechanism at
    all. The model already sees every registered tool on any chat turn
    that offers tools (`available_tool_specs()`), so recognizing "I have
    nothing for this" is just the model's own reasoning -- what was
    actually missing was somewhere for that recognition to *go* instead
    of evaporating at the end of one chat reply. That's the whole
    milestone: `report_capability_gap` (`app/plugins/builtin/capability_gap.py`,
    always registered, SAFE_ACTION) and `CapabilityGapStore`
    (`app/capabilities/service.py`) turn the model's own gap-spotting
    into a persisted, queryable development spec -- `requested_capability`
    plus, when the model can supply them from its own general knowledge,
    a `suggested_integration` (e.g. "Network UPS Tools (NUT)" for UPS
    battery health) and a `suggested_tool_spec` sketch (e.g.
    `{"name": "ups_get_status", "parameters": {...}}`). `GET
    /siris/capability_gaps` exposes the resulting backlog, same
    direct-read relationship to the tool as goals/routines/events already
    have to theirs.

    Ruled out a different design, worth recording why: an LLM-*calling*
    tool (something that itself queries the model to reason about a gap)
    would have hit a real architectural wall -- `Tool.run()` is
    synchronous but `LLMClient.generate()` is async, and route handlers
    already call `run_tool()` synchronously from inside a running event
    loop, so bridging with `asyncio.run()` inside a tool would raise
    "cannot be called from a running event loop." Not building a second
    LLM-calling path avoided that entirely, and is arguably more honest
    too: the model doing this reasoning already IS an LLM call, the one
    already in flight for the chat turn -- a nested one would just be
    redundant. This does mean tool-spec quality depends on how good the
    underlying model's own domain knowledge is; that's an accepted
    characteristic, not hidden.

    No new detection engine also means 22/23/24 relate to this milestone
    as context rather than hard dependencies: 22's introspection is what
    lets the model (and a human) see the current tool list to compare a
    request against, and 23/24 are where a gap is likely to surface in
    practice (a goal or multi-step plan hitting a wall), but nothing here
    required them to be built first.

    Verified: unit tests for `CapabilityGapStore` (with/without the
    optional fields, most-recent-first ordering), endpoint tests (tool
    registration, the full tool-call round trip populating
    `GET /siris/capability_gaps`, the tool showing up under its own
    "capabilities" category), and a live `uvicorn` run driving the exact
    scenario from the roadmap's own example through a real mock Ollama
    backend: asked "can you check if my UPS battery is healthy?", got
    back a real `report_capability_gap` call naming Network UPS Tools and
    a concrete `ups_get_status` tool spec, and confirmed it was
    genuinely persisted and queryable afterward via
    `GET /siris/capability_gaps` -- a real spec someone could hand to
    Claude Code working this repo. Full suite: 206 passed.
32. **Done** -- Automatic tool discovery. `POST /siris/integrations`
    ingests an OpenAPI 3.x spec (either `spec_url`, fetched once at
    registration time, or `spec` inline) and generates one `Tool`
    instance per GET operation, rather than every integration being a
    hand-written `app/<service>/client.py` + `app/plugins/builtin/<service>.py`
    pair like 13's six. The scope cut flagged before this milestone
    started was taken further than "default to READ" once actually
    building it: GET-only, full stop -- v1 doesn't discover
    POST/PUT/PATCH/DELETE operations *at all*, rather than discovering
    them and merely defaulting their permission. Generating a tool that
    could mutate an arbitrary API from spec text alone and then handing
    it a permission level, even a conservative one, would be misleading
    about what's actually safe to let a model call unattended -- there's
    no "upgrade a specific generated tool" mechanism yet, so nothing
    stronger than READ gets generated until there's real review behind
    that.

    `app/integrations/openapi.py`'s `discover_get_operations()` is a
    deliberately hand-rolled minimal parser, not a full OpenAPI library
    -- it doesn't resolve `$ref` or schema composition
    (allOf/oneOf/anyOf); a parameter using those falls back to a plain
    string type rather than failing the whole import, since a
    slightly-imprecise tool is more useful than none. `IntegrationStore`
    (`app/integrations/service.py`) persists the raw spec (not just the
    parsed operations) so tools regenerate on every startup --
    `SirisAI._register_discovered_integrations()` re-parses and
    re-registers everything on construction, the same "conditionally
    register from stored config" pattern every other integration in this
    app already follows, just DB-driven instead of env-driven.
    `auth_header_value` is treated as a credential throughout -- excluded
    from `IntegrationStore.list_integrations()`'s default (public) path
    and from every route/tool-facing response; only read back internally
    when SirisAI rebuilds an authenticated client at startup.

    A real bug caught before it shipped, worth recording: the first pass
    built one `httpx.Client()` per discovered *operation*. Constructing an
    `httpx.Client()` turned out to be surprisingly expensive in this
    project's heavily-proxied network environment (~50ms each) --
    30 operations meant 30 clients meant entire seconds added to every
    `SirisAI()` construction once a few integrations had accumulated,
    caught by the test suite's runtime visibly jumping from ~6s to ~78s.
    Fixed by sharing one client per *integration* instead
    (`build_dynamic_tools()`), which is also just more correct (one
    connection pool per API, not thirty) -- down to ~30s. The remaining
    increase over the ~6s baseline is an accepted, understood cost of
    this test suite's existing shared-database convention (many tests
    across the whole suite construct fresh `SirisAI()` instances against
    the same DB this milestone's tests add integrations to), not a new
    problem -- documented rather than chased further with test-isolation
    changes that would ripple into every other test file's established
    pattern.

    Verified: unit tests for the parser (GET-only discovery, path/query
    param extraction, the `$ref` string-type fallback, a derived name
    when `operationId` is missing), `DynamicOpenAPITool` (path
    substitution, query forwarding, the configured auth header actually
    sent, one client genuinely shared across every operation, always
    READ), `IntegrationStore` (secret exclusion by default, included with
    `include_secrets=True`), endpoint tests (immediate usability after
    registration, 422 for no spec/no GET operations, duplicate names
    skipped rather than crashing, secrets never appearing in any
    response), and a live `uvicorn` run against a real mock "Network UPS
    Tools" API and OpenAPI spec server -- registered by fetching a real
    `spec_url` over real HTTP, called the discovered
    `nut_get_battery(ups_name="main-ups")` tool and got back real battery
    data with the configured `X-API-Key` header actually verified by the
    mock server, confirmed the secret never appeared in
    `GET /siris/integrations`, and confirmed the discovered tools
    survived a full process restart with zero re-registration calls.
    Full suite: 223 passed.
33. **Done** -- Cortex. A capstone, not a new capability: once 21/23/24/28
    exist, the request -> context -> plan -> memory/world-model -> tools ->
    verify -> memory -> response pipeline described in the brainstorm is
    mostly already sitting across `ContextEngine` (16), the planner (24),
    and `SirisAI.run_tool()` (11) -- this milestone names and consolidates
    that flow into one orchestration path, `app/cortex/orchestrator.py`'s
    `Cortex` class, so `/siris/chat` calls into it instead of assembling
    context, calling the model, and handling tool calls inline the way it
    used to.

    Before this milestone, `POST /siris/chat` (app/api/routes/siris.py) did
    all of the work itself: build context, pick a model via
    `model_router`, branch on `use_planner`, run a tool through
    `run_tool()`, make the follow-up LLM call for a final answer -- with
    two private helper functions (`_execute_tool_call`,
    `_run_planned_chat`) doing the heavy lifting inline in the route
    module. `Cortex.process()` now owns that whole pipeline, and
    `Cortex.confirm()` owns the `/siris/chat/confirm` half (running an
    already-approved tool and making the same follow-up call). Both return
    a plain dataclass (`CortexResult`) with no FastAPI/pydantic awareness --
    `error`/`permission_denied` fields stand in for what used to be a
    directly-raised `HTTPException`, so Cortex's contract is "always return
    a result" the same way `run_tool()`'s callers already have to handle
    `ConfirmationRequired`/`PermissionError`/`Exception` as outcomes rather
    than assuming success. The route module shrank to essentially:
    create/resume a conversation, call `Cortex`, map the `CortexResult`
    into a `ChatResponse` (raising the right `HTTPException` for `error`).

    Deliberately not a new subsystem: no new persistence, no new tools, no
    behavior change for any existing caller -- same request/response shapes,
    same single-tool-call-per-turn default, same `use_planner` opt-in, same
    audit triggers recorded. The actual outcome is architectural: the LLM
    client is no longer something a route handler touches directly (it's
    resolved by `model_router` and used only inside `Cortex`), so a second
    orchestration entry point (a future voice pipeline, a CLI, a scheduled
    routine that wants the full context+tools+verify flow rather than a
    single direct tool call) can call `Cortex.process()` the same way
    `/siris/chat` does, without re-deriving this pipeline inline the way
    the route used to.

    Verified: full suite (228 passed, up from 223) including 5 new direct
    unit tests against `Cortex` (`tests/test_cortex.py` -- plain-text
    response persists the turn, a tool call gets a real follow-up
    round-trip, `use_planner=True` delegates to the `Planner` and returns
    its steps as `plan`, an LLM backend failure comes back as an `error`
    result rather than raising, `confirm()` runs and persists without a
    fresh user turn) plus every pre-existing chat/planner/confirm/context
    endpoint test passing unmodified, since the route's HTTP contract
    didn't change. Live-verified against a real `uvicorn` process and a
    real mock Ollama `/api/chat` server (`mock_ollama.py`): a plain prompt,
    a single-tool-call-with-follow-up prompt, and a `use_planner=true`
    prompt that chains two `echo` calls all produced correct responses over
    real HTTP, and `GET /siris/conversations/{id}` / `GET /siris/activity`
    confirmed the persisted transcript and audit trigger
    (`"chat: model chose this tool while responding to the user's
    request"`) are unchanged from before the refactor.

Milestone 33 closed out the second roadmap batch (21-33). What follows
isn't from either brainstorm -- a direct request to integrate with a
specific piece of the user's actual homelab, given the same "what was
deliberately left out, and why" treatment as everything above.

34. **Done** -- Hermes Agent integration. Hermes Agent is a separate agent
    already running on the same host as SirisAI, with its own memories,
    skills, and terminal access -- including Docker container control.
    Rather than SirisAI reimplementing what Hermes can already do, this
    adds one delegate tool, `hermes_run_task` (`app/hermes/client.py`,
    `app/plugins/builtin/hermes.py`): SirisAI hands it a natural-language
    instruction, Hermes does its own multi-step reasoning/tool use, and
    reports back what it did.

    `HermesClient` talks an OpenAI-style chat completions endpoint
    (`POST {base_url}/v1/chat/completions`) -- the convention most
    self-hosted agent/inference servers already speak (llama.cpp server,
    vLLM, LiteLLM proxies, ...), and the shape confirmed for Hermes's
    actual API. No auth configured on Hermes's side currently, so the
    client is unauthenticated; `SIRISAI_HERMES_BASE_URL` /
    `SIRISAI_HERMES_MODEL` are the only new settings, following the same
    "empty base_url = tool not registered" convention as every other
    optional integration here. If Hermes's real endpoint path or request
    shape ever turns out to differ, `HermesClient` is the one place to
    adjust it -- the tool never talks to httpx directly.

    Permission tier is `SAFE_ACTION`, not `CONFIRM_REQUIRED` or
    `RESTRICTED`: the same tier already used for other routine homelab
    operations here (e.g. `DockerRestartContainerTool`) -- it runs
    immediately without a confirmation round-trip, but every call is still
    fully logged to the audit trail (`GET /siris/activity`) with its
    trigger and the policy that allowed it, same as any other tool.
    Deliberately one delegate tool rather than several Hermes-specific
    ones (`hermes_restart_container`, `hermes_run_command`, ...): the
    actual capability lives on Hermes's side, already exposed through its
    own memories and skills, so mirroring that surface here would just be
    a second, thinner copy of it that drifts as Hermes's own capabilities
    change.

    Verified: full suite (233 passed, up from 228) including 5 new tests
    (`tests/test_hermes.py`: the client posts the correct OpenAI-style
    payload and extracts the response content, handles an empty
    `choices` list, the tool wraps the client's result and is
    `SAFE_ACTION`) plus two registration-gating tests in
    `tests/test_tool_library_registration.py` (not registered by default,
    registered when `hermes_base_url` is set). Live-verified against a
    real `uvicorn` process and a real mock Hermes server
    (`mock_hermes.py`) speaking the actual `/v1/chat/completions` shape:
    `GET /siris/capabilities` grouped `hermes_run_task` under its own
    `"hermes"` category, `POST /siris/tools/hermes_run_task/run` sent a
    real instruction over real HTTP and returned Hermes's real reply,
    `GET /siris/activity` confirmed the correct trigger
    (`"direct API request..."`) and policy (`"SAFE_ACTION: routine and
    reversible, runs immediately without confirmation"`), and a second run
    with both a mock Hermes and a mock Ollama backend confirmed
    `hermes_run_task` is offered to the model like any other tool via
    `GET /siris/status`.

35. **Done** -- Hardening pass: tool-result round-tripping. Milestone 10
    explicitly flagged this gap and left it as future work: a tool
    call/result from an earlier turn was persisted (visible via
    `GET /siris/conversations/{id}`) but never replayed to the model on a
    later turn -- only the plain user/assistant text was. In practice
    that meant a follow-up question like "what did you just do?" had
    nothing to go on beyond whatever the model happened to say in its own
    prior reply; the actual tool call and its result were invisible to it.

    `Cortex._load_messages()` (`app/cortex/orchestrator.py`) now expands
    each persisted "tool" row back into the same assistant-`tool_calls` +
    `tool`-result message pair this module already builds mid-turn
    (`_run_single_tool`'s follow-up call, `_run_planned`'s planner loop),
    interleaved in original chronological order alongside user/assistant
    text. Since `Cortex` is the one place both `/siris/chat` and
    `/siris/chat/confirm` load history from (milestone 33), this fixes
    the gap for the planner path too, not just the single-tool-call one.
    No format change, no new persistence -- `Message.tool_name`/
    `tool_arguments`/`content` already carried everything needed; this
    just stopped discarding them when building history for a new call.

    Verified: full suite (235 passed, up from 233) including a new
    `tests/test_cortex.py` test asserting the exact replayed message
    shape reaches a recording fake `LLMClient` on a second `Cortex.process()`
    call, and a new `tests/test_conversation.py` endpoint-level test doing
    the same over two real `POST /siris/chat` calls. Live-verified against
    a real `uvicorn` process and a real mock Ollama server logging every
    raw request body it received: a "please echo ping" turn followed by
    "what did you just do?" showed the second call's actual HTTP payload
    contained the full prior exchange (user prompt, the assistant's
    `tool_calls`, the tool's `{"echo": "ping"}` result, and the
    intervening assistant reply) ahead of the new user message -- and the
    mock's own reply ("You asked me to echo 'ping' earlier.") confirmed
    it could actually see and use that history, not just receive it.

36. **Done** -- Hardening pass: planner resume after confirmation.
    Milestone 24 explicitly flagged this as out of scope: a plan that hit
    a `CONFIRM_REQUIRED` tool mid-run stopped there, and `POST
    /siris/chat/confirm` only ever ran that one confirmed step -- any
    further steps the plan would have taken never happened, silently
    ending what looked like a multi-step task after just its first
    approval-gated action.

    `Cortex.confirm()` (`app/cortex/orchestrator.py`) gained an opt-in
    `use_planner` parameter, threaded through from a new
    `ConfirmToolRequest.use_planner` field on `POST /siris/chat/confirm`.
    When true, after the confirmed tool runs, its result is fed into the
    same kind of loop that stopped for it -- `Planner.run()` picks up from
    there via `Cortex._run_planned()` (the same method the `use_planner`
    path of `POST /siris/chat` already uses), so it can call further
    tools, hit another `CONFIRM_REQUIRED` tool and stop again (fully
    supported -- a plan needing two approvals in a row just means two
    `POST /siris/chat/confirm` calls, each with `use_planner: true`), or
    produce a final answer. The response's `plan` is the confirmed step
    followed by whatever the continuation executed -- this turn's work,
    same "this turn only" semantics `plan` already has on `POST
    /siris/chat`; earlier steps from before the confirmation are still
    visible via `GET /siris/conversations/{id}`, not repeated. Default is
    `false`: confirming a single non-planned tool call, or a planner
    confirmation without opting into continuation, behaves exactly as
    before -- one follow-up call, done.

    No new persisted state -- the "no server-side pending-action state"
    design principle from milestone 24/`POST /siris/chat/confirm`'s own
    docstring holds: the client already knows to pass `use_planner: true`
    because the response that carried `confirmation_required` also
    carried `plan` (not `tool_call`) in the first place.

    Verified: full suite (239 passed, up from 235) including three new
    `tests/test_cortex.py` tests (continuation runs a further tool call
    then produces a final answer; continuation immediately hits a second
    `CONFIRM_REQUIRED` tool and correctly surfaces the fresh
    `pending_confirmation` alongside the already-confirmed step in `plan`;
    confirming without `use_planner` is unchanged) and one new
    `tests/test_planner.py` endpoint-level test exercising the full
    two-request HTTP flow. Live-verified against a real `uvicorn` process,
    a real mock Ollama backend, and a real mock Home Assistant server
    using the actual `home_assistant_call_service` `CONFIRM_REQUIRED`
    tool (not a test-only stand-in): a planner turn stopped for
    confirmation on a real service call, confirming with `use_planner:
    true` ran that call against the mock Home Assistant API, continued on
    to a second real tool call, and produced a final answer -- with
    `GET /siris/conversations/{id}` showing the full four-message
    sequence persisted correctly and `GET /siris/activity` showing the
    confirmed step's trigger ("user explicitly confirmed...") distinct
    from the continuation step's ("planner: step 1 of an in-progress
    plan"), each with its own correct audit policy.

37. **Done** -- Hardening pass: event-timing anomaly detection.
    Milestone 19's original roadmap entry wanted true anomaly detection
    over accumulated history -- "the backup normally finishes by 3am but
    hasn't today" -- and explicitly cut it because a fresh install has no
    history to detect a pattern in. Milestone 27 later built exactly this
    shape of statistical baseline, but only for metrics
    (`MetricsStore.baseline()`); this applies the same idea to event
    timing, closing milestone 19's own example.

    `EventMemoryStore` (`app/memory/events.py`) gained
    `distinct_recurring_signatures()` (which `(description, entity_name)`
    pairs have occurred often enough among recent history to be worth
    checking -- a one-off event never accumulates enough occurrences to
    even become a candidate, so nothing needs to be explicitly registered
    as "recurring") and `timing_baseline()` (mean/stdev interval between
    a signature's occurrences, `None` below `min_samples` -- same honest
    scope-cut as `MetricsStore.baseline()`, a fresh signal has no history
    to be meaningfully overdue against). A new `EventTimingSkill`
    (`app/skills/builtin/event_timing.py`, `event_timing_check` tool,
    copying `ServerHealthSkill`'s own "copy this file's shape for the
    next skill" invitation) flags a signature overdue when the time since
    its last occurrence exceeds `mean + 2*stdev` -- and composes into
    `POST /siris/proactive/check` (`app/api/routes/proactive.py`)
    alongside `server_health_check`, its warnings feeding the same
    `NotificationManager` pass (a new `"timing"` category, classified the
    same deterministic way as the resource/docker ones already were).

    A real bug caught live before it shipped, worth recording: the first
    pass had no floor on how small a baseline's mean interval could be.
    Verifying against a real `uvicorn` process, 5 events posted back to
    back over the real `POST /siris/events` bus (seconds apart) came back
    "overdue" a few hundred milliseconds later in `POST
    /siris/proactive/check` -- a burst of rapid-fire events (retries, a
    webhook firing repeatedly) has a near-zero mean/stdev interval, so
    *any* later gap, even milliseconds, is orders of magnitude past
    `mean + 2*stdev`. Fixed by treating a sub-1-hour mean interval as not
    a schedule at all (`_MIN_MEAN_INTERVAL_HOURS`) -- a precondition for
    treating a signature as recurring-on-a-schedule, not an edge case of
    the threshold math. This also would have caused real flakiness in one
    of the unit tests below (a test seeding 5 occurrences at the sample
    threshold, inserted within the same test function, i.e. a burst by
    construction) had it run before the fix landed -- caught the same way
    milestone 32's httpx.Client() bug was: by actually running the thing
    against real HTTP and real elapsed time, not just unit tests with
    fully controlled clocks.

    `log_event()` gained an optional `created_at` override (only for
    tests constructing a specific occurrence history, same testability
    pattern as `RoutineStore.mark_run()`'s `run_at`; real callers never
    pass it) and `EventTimingSkill.run()` accepts an optional `now`
    (same pattern as `RoutineStore.due_routines()`) so tests control
    "is this overdue" without depending on wall-clock time.

    Verified: full suite (251 passed, up from 239) including 12 new
    `tests/test_event_timing.py` tests (signature discovery respecting
    the sample threshold and entity scoping, baseline mean/stdev
    computation, the skill correctly flagging an overdue event and not
    flagging an on-time one, the burst-of-rapid-fire-events regression
    test above, `NotificationManager` classifying the new message shape
    as `"timing"`, and endpoint-level coverage of the composed
    `POST /siris/proactive/check`) plus updated fakes in
    `tests/test_proactive.py`/`tests/test_explainable_autonomy.py` to
    handle the new `event_timing_check` call. Live-verified against a
    real `uvicorn` process: logged real events over the real event bus,
    confirmed `event_timing_check` and its `"timing"` classification are
    wired correctly end to end via `GET /siris/status`,
    `GET /siris/capabilities`, and `POST /siris/proactive/check`, and
    (after the fix) confirmed the burst scenario no longer produces a
    false positive.

38. **Done** -- Hardening pass: deterministic per-tool verification.
    Milestone 24's own writeup was explicit about what its "verification"
    actually was and wasn't: the model re-assessing tool result content
    against the original request, not "a separate deterministic
    verification pass that can tell 'this 200 OK doesn't actually mean
    the service is healthy' independent of the model's own judgment" --
    that needed per-tool success criteria the milestone deliberately
    didn't define, to avoid overclaiming a rigor that wasn't there. This
    defines it.

    `Tool` (`app/plugins/base.py`) gains an optional `verify(arguments,
    result) -> ToolVerification` hook -- same shape and convention as
    `dry_run()` (milestone 25): raises `NotImplementedError` by default
    (most tools have no meaningful post-condition beyond "the call
    succeeded," already covered by `run()` not raising), override only
    where a real gap exists. `SirisAI.run_tool()` calls it after every
    successful `run()`, best-effort (`NotImplementedError` means
    unsupported; any other exception is logged and swallowed -- a broken
    verifier doesn't take the tool call down with it), and records the
    outcome to the *same* audit entry as two new nullable columns,
    `verified`/`verification_detail` (same "extend what's already
    recorded" principle milestone 28 established for
    trigger/evidence/policy) -- never mutates `run_tool()`'s own return
    value or raises on its own account: a failed verification is
    information about the outcome, not a reason to treat the call as
    having failed. `GET /siris/activity` and the `explain_recent_action`
    tool (28) both expose the new fields for free, since both already
    just serialize the full audit entry.

    `DockerRestartContainerTool.verify()` is the reference implementation
    -- and the literal scenario milestone 24's own text used as its
    example: Docker's `/restart` call can return 200 even though the
    container's entrypoint crashes immediately back out, a real and
    common failure mode a raw HTTP-success check can't see. One state
    check right after the restart call returns, not a health monitor --
    explicitly does NOT catch a slower crash-loop (fine for a few
    seconds, then dies), which would need polling over time; same
    "don't overclaim rigor that isn't there" honesty as the thing it's
    closing the gap on.

    Deliberately scoped to the audit log, not threaded into the same-turn
    model context (the planner's `working_messages`, the chat follow-up
    call): doing that would mean reshaping what every tool call returns
    across three different code paths for a benefit -- automatic
    same-turn awareness -- that `explain_recent_action` already gives the
    model on demand, and a human already gets via `GET /siris/activity`.
    A real, deterministic, independent verification pass now exists and
    is always computed; making the model *automatically* see it inline
    is a separate, larger feature left as its own future boundary rather
    than folded in here.

    Verified: full suite (261 passed, up from 251) including 10 new tests
    (`tests/test_verification.py`: the default raises `NotImplementedError`,
    an override doesn't affect `run()`'s own return value,
    `run_tool()` records nothing for a tool without a verifier, records a
    successful verification, records a *failed* verification without
    treating the call itself as failed -- the core case this exists for
    -- tolerates a broken verifier without crashing, and the activity
    endpoint exposes the new fields; three new `tests/test_docker.py`
    tests for the reference implementation: verified when running,
    correctly flagged when exited, correctly flagged when the container's
    gone missing after restart). Live-verified against a real `uvicorn`
    process and a real mock Docker Engine API: restarting a container
    that came back `running` recorded `verified: true`; restarting one
    the mock then reported as `exited` (simulating the exact "the restart
    call succeeded, the container still isn't up" scenario) recorded
    `verified: false` with a real, specific reason -- while `result`
    (`{"restarted": true}`) and the absence of `error` show the tool call
    itself was never treated as having failed -- confirmed visible via
    both `GET /siris/activity` and a live `explain_recent_action` call.

The roadmap below (39-48) is a third batch -- not from a structured
brainstorm like 21-33, but from directly asking "what would make this
feel more like Jarvis, functionality-wise" and picking the answers that
map to real, buildable capability rather than sci-fi flavor. Same rule as
every batch before it: nothing here is committed to until its own
milestone lands, with the same "what was deliberately left out, and why"
treatment on completion. A few of these (44's escalation channel, 46's
user-identity mechanism, 48's satellite dependency) have an open design
question the roadmap entry itself flags rather than one this batch
silently decided -- same posture as milestone 29's routine engine.

39. **Done** -- Morning/evening briefing routine. Composes what already
    exists -- calendar (`CalendarClient`), weather (`WeatherClient`), the
    proactive check pipeline (`server_health_check` + `event_timing_check`),
    and active goals -- into one digest via a new `DailyBriefingSkill`
    (`app/skills/builtin/daily_briefing.py`, `daily_briefing` tool), same
    shape as `ServerHealthSkill` ("copy this file's shape for the next
    skill"). READ-only and side-effect-free, same division of labor
    milestone 19 already drew: the skill composes and summarizes, a new
    `POST /siris/proactive/briefing` (`app/api/routes/proactive.py`,
    mirroring `/check`) decides to send it, via the existing `notify`
    tool if a channel is configured -- always sends when one is (a
    briefing is the point of calling this at all, not a conditional
    alert), no `NotificationManager` pass since there's nothing to score
    or correlate. Same "no in-process scheduler" boundary as `/check`:
    something external decides the cadence. ("Goals due soon" was the
    original brainstorm framing, but `Goal` has no due-date field --
    this surfaces what's still *active* instead, an honest match for
    what the data actually supports.)

    A real bug caught live before it shipped: the first pass called
    `weather_current`/`calendar_upcoming_events` with no error handling,
    so an unreachable weather API or CalDAV server 500'd the *entire*
    endpoint -- losing the warnings and active-goals sections that had
    nothing to do with weather. Weather/calendar are enrichment, not the
    point of the briefing; fixed by wrapping each in its own best-effort
    try/except, the exact same precedent `DockerRestartContainerTool
    .dry_run()`'s Plex session check already set ("an unreachable Plex
    server doesn't fail the dry run, it just omits that detail").

    Verified: full suite (273 passed, up from 261) including 12 new
    `tests/test_daily_briefing.py` tests (omits weather/calendar when
    not configured, includes each when configured, reports "nothing on
    the calendar" for an empty day, merges health + event-timing
    warnings, lists active goals, the weather/calendar-unreachable
    regression tests, tool registration, and endpoint-level notify/no-notify
    coverage). Live-verified against a real `uvicorn` process and a real
    mock ntfy server: composed a real briefing with a real created goal,
    confirmed `POST /siris/proactive/briefing` sent a real notification
    to the mock server with the exact summary text -- and, after
    pointing `SIRISAI_WEATHER_LATITUDE`/`LONGITUDE` at real coordinates
    (the sandbox's own network policy blocks the real open-meteo API,
    which is what surfaced the bug above in the first place), confirmed
    the endpoint now returns 200 with a complete, correctly-degraded
    briefing and still sends the notification, instead of 500ing.
    Calendar's own live behavior against a real CalDAV server remains
    unverified here, same acknowledged limitation `CalendarClient`'s own
    docstring already states.

40. **Done** -- Goal check-ins. Goals (23) were passive storage --
    nothing ever looked at one again once created. `GoalStore.stale_goals()`
    (`app/goals/service.py`) is a deterministic staleness check (active,
    not touched in >= 14 days) and a new `GoalCheckInSkill`
    (`app/skills/builtin/goal_check_in.py`, `goal_check_in` tool) wraps
    it, same shape as `ServerHealthSkill`/`EventTimingSkill`. Composes
    into `POST /siris/proactive/check` alongside `server_health_check`/
    `event_timing_check`, feeding the same `NotificationManager` pass (a
    new `"goals"` category) -- "still working on X?" surfaces on its own
    instead of only ever being asked.

    A real bug caught during implementation, before it ever needed live
    testing to surface: `Goal.updated_at` has SQLAlchemy's `onupdate=`,
    which only fires on an actual `UPDATE` to the `goals` row itself --
    `add_objective()`/`complete_objective()` only ever touched a
    `GoalObjective` row, never the `Goal` row. A goal getting steady
    objective work for weeks would still look completely untouched by
    every measure `stale_goals()` has, false-flagging exactly the kind of
    goal this feature exists to *not* bother you about. Fixed by having
    both explicitly set `goal.updated_at = datetime.now(UTC)` -- real
    progress on a goal now counts as activity, not just a status change.

    Verified: full suite (284 passed, up from 273) including 5 new
    `GoalStore` tests in `tests/test_goals.py` (a fresh goal excluded, an
    untouched old one included, a non-active old goal excluded even
    though it qualifies by age, and the two regression tests proving
    `add_objective`/`complete_objective` actually un-stale a goal) and 7
    new `tests/test_goal_check_in.py` tests (the skill flagging/not-flagging,
    healthy with no active goals, `NotificationManager` classifying the
    new message shape as `"goals"`, tool registration, and the composed
    endpoint). Live-verified against a real `uvicorn` process: created a
    real goal via the real API, backdated it directly in the live SQLite
    file to simulate 30 days of inactivity, confirmed `goal_check_in`
    correctly flagged it, called the real `add_goal_objective` tool and
    confirmed it immediately un-staled -- then backdated it again and
    confirmed `POST /siris/proactive/check` composed the warning
    correctly with the new `"goals"` classification end to end.

41. **Done** -- Behavioral pattern anomalies. The same mean/stdev-over-
    history mechanism `event_timing_check` (37) already built for
    infrastructure events ("the backup didn't run") applied to logged
    user-triggered events instead ("the office lights are usually off by
    11, they're still on"). Genuinely zero new detection logic: the
    entire milestone is `HomeAssistantCallServiceTool.run()`
    (`app/plugins/builtin/home_assistant.py`) now also logging an
    `Event` after a successful call, grouped as `"{domain}.{service}"`
    (e.g. `"light.turn_off"`) with `entity_name=entity_id` (e.g.
    `"light.office"`) -- the exact `(description, entity_name)` shape
    `EventMemoryStore.distinct_recurring_signatures()`/`timing_baseline()`
    already group and baseline by. Once "the lights usually go off around
    11pm" exists as a recurring Event signature, `event_timing_check`
    already knows how to notice it's late, exactly the same way it
    already notices a backup didn't run -- extends what 37 built, doesn't
    reinvent it. Deliberately scoped to this one tool (not every tool
    call): `home_assistant_call_service` is the concrete, homelab-relevant
    "user behavior" action the brainstorm's own example was about; making
    *every* tool call generate an Event would blur the Event/ToolExecution
    distinction the codebase already draws (an Event is a thing that
    happened, a ToolExecution is SirisAI's own internal record of what it
    did -- see `app/db/models.py`).

    Verified: full suite (286 passed, up from 284) including 2 new
    `tests/test_home_assistant.py` tests (a call logs the expected
    Event with the right description/entity_name/source; a call with no
    `entity_id` logs one with `entity_name: null`) plus the existing
    `call_service` test updated for the tool's new constructor
    dependency. Live-verified against a real `uvicorn` process and a
    real mock Home Assistant server: a real `home_assistant_call_service`
    call produced a real Event visible via `GET /siris/events`; then,
    after seeding six realistic nightly occurrences directly in the live
    SQLite file (most recent ~30h ago, well past a ~24h rhythm), a real
    call to `event_timing_check` correctly flagged `"light.turn_off" on
    "light.office"` as overdue -- the literal "office lights are usually
    off by now, they're still on" scenario, using nothing but the
    existing machinery.

42. **Done** -- Presence-triggered scenes. UniFi presence (who's
    connected) composed with a Home Assistant scene call -- "everyone
    left" -> away scene, "someone's home" -> welcome scene, via a new
    `PresenceSkill` (`app/skills/builtin/presence.py`, `presence_check`
    tool, registered `SAFE_ACTION` -- a real side effect, but a narrow,
    pre-configured one, same reasoning as `docker_restart_container`).
    Deliberately not a general rules engine -- exactly two conditions and
    one action each, not an arbitrary trigger/action DSL this milestone
    doesn't need. New `SIRISAI_PRESENCE_DEVICE_NAMES` (matched against
    the same `name` field `unifi_list_clients` already reports -- not a
    MAC address, since `UniFiClient` is this codebase's least-verified
    integration and matching by name keeps this milestone from touching
    it further), `SIRISAI_PRESENCE_HOME_SCENE`/`_AWAY_SCENE` (empty =
    transitions still detected and logged, just no scene activated).
    Transition-only by design: reuses `EventMemoryStore` (same "no new
    store" reasoning as 41) to remember the last known state and only
    acts on an actual change, not every cron tick -- a first-ever check
    establishes a baseline rather than guessing. A new
    `POST /siris/proactive/presence` (404 if not configured) runs it, on
    its own schedule since presence wants a much shorter interval than a
    health check.

    The one real design call worth calling out: `home_assistant_call_service`
    is `CONFIRM_REQUIRED` (arbitrary/unbounded, real physical
    consequences) -- `PresenceSkill` calls it with `confirmed=True`
    anyway, deliberately. It's never calling an arbitrary command, only
    ever the one specific scene the user configured ahead of time via
    `SIRISAI_PRESENCE_*_SCENE`; setting that value *is* the approval, the
    same reasoning `SIRISAI_RESTRICTED_TOOLS_ENABLED` already uses to
    pre-approve specific tools via config instead of a live confirmation
    every call. The audit trail stays fully explainable regardless --
    `trigger` on that specific call spells out exactly which setting
    authorized it, distinct from every other way `home_assistant_call_service`
    can get invoked.

    Verified: full suite (297 passed, up from 286) including 11 new
    `tests/test_presence.py` tests (first-check baseline with no scene
    call, both transition directions correctly activating their scene,
    no-op on no change, a transition still detected/logged without a
    configured scene, Home Assistant not configured skipping the scene
    call gracefully, registration gating, and the endpoint including its
    404). Live-verified against a real `uvicorn` process, a real mock
    UniFi server (with a `/control` endpoint to flip its reported clients
    between runs without restarting), and a real mock Home Assistant
    server: first check established the baseline with no scene call;
    flipping the mock to "away" and checking again correctly activated
    `scene.away` and logged the transition; flipping back to "home"
    correctly activated `scene.welcome_home` -- confirmed via
    `GET /siris/events` (the two real transitions logged) and
    `GET /siris/activity` (the `presence_check` call and the
    auto-confirmed `home_assistant_call_service` call beneath it, with
    `trigger` correctly naming the exact setting that authorized it).

43. **Done** -- Actionable push notifications. ntfy supports action
    buttons in its payload -- a real gap this closed: a routine
    (milestone 29) whose action hit a `CONFIRM_REQUIRED` tool used to
    just record the `ConfirmationRequired` message as an error and drop
    it, since a routine runs unattended (an external cron hits
    `run_due`, not a human watching a chat turn) and nothing else ever
    surfaced it. `RoutineRunner._notify_pending_confirmation()`
    (`app/routines/runner.py`) now sends a push notification with a
    one-tap "Confirm" action button for exactly that case.

    Not `POST /siris/chat/confirm` as originally framed in the
    brainstorm -- a routine-originated confirmation has no
    `conversation_id` (routines aren't chat turns) to hand that
    endpoint. The button targets `POST /siris/tools/{tool_name}/run`
    with `confirmed: true` instead: already the conversation-independent
    way to run a tool directly, exactly what a notification tap with no
    chat session behind it actually is.

    `NotifyClient` (`app/notify/client.py`) switched from ntfy's simple
    header-based publish to its JSON publish endpoint -- a fully
    equivalent, equally well-supported mode, needed because it's the
    only one that can carry `actions`
    (https://ntfy.sh/docs/publish/#action-buttons). `NotifyTool` accepts
    `actions` as a plain kwarg for this internal use, but deliberately
    doesn't declare it in the tool's own LLM-facing parameter schema --
    constructing an exact ntfy action-button payload (URL, method,
    headers, body) is a programmatic concern for a caller that already
    knows the target endpoint, not something worth prompting the model to
    assemble. New `SIRISAI_PUBLIC_BASE_URL` (the externally-reachable
    base this instance is served at -- `SIRISAI_HOST`/`PORT` are for
    binding, not necessarily what a phone tapping the button can reach)
    -- empty means the notification still sends, just without the
    button. The `Authorization` header on the action itself reuses
    `SIRISAI_API_KEY` when configured, since the button's request has to
    clear the same auth every other `/siris/*` call does.

    Verified: full suite (303 passed, up from 297) including 2 updated
    `tests/test_notify.py` tests for the new JSON wire format, 2 new
    ones covering `actions` (included when given, omitted when not), and
    4 new `tests/test_routines.py` tests (no notification without a
    configured channel, a correctly-shaped action button when
    `SIRISAI_PUBLIC_BASE_URL` is set, the action omitted without it, and
    the `Authorization` header omitted without an API key). Live-verified
    against a real `uvicorn` process, a real mock ntfy server, and a real
    mock Home Assistant server: created a real routine whose action was
    `home_assistant_call_service`, ran it, confirmed the mock ntfy server
    received the exact JSON payload with a correctly-shaped `http` action
    (real public URL, real `Authorization: Bearer` header, real JSON
    body) -- then replayed that *exact* request (the same one a phone
    tapping the button would send) against the running instance and
    confirmed it actually ran the tool against the mock Home Assistant
    server, with `GET /siris/activity` showing the complete, correctly
    explainable chain: blocked pending confirmation -> notified with
    evidence -> explicitly confirmed and run.

44. **Done** -- Escalation tiers beyond notify. This entry's open
    question (ntfy-priority-only vs. a real second channel needing a
    third-party provider and the user's own credentials) was decided in
    favor of ntfy-priority-only -- no new external dependency. Every
    proactive notification used to go out at the exact same ntfy
    priority regardless of severity: `NotificationManager`'s (26)
    `"urgent"` classification only ever changed the notification's
    *title* ("SirisAI: urgent" vs. "SirisAI: server health check"), not
    anything that could actually break through a phone's silent/do-not-
    disturb settings the way ntfy's own priority levels are designed to.

    `NotifyClient.send()` (`app/notify/client.py`) gained a `priority`
    parameter -- ntfy's own vocabulary (`min`/`low`/`default`/`high`/`urgent`,
    passed straight through unvalidated, same "let a mismatch fail loudly
    against the real service" posture as this codebase's other thin
    clients) -- and `POST /siris/proactive/check`
    (`app/api/routes/proactive.py`) now sets `priority: "urgent"`
    whenever any item it's sending was classified `"urgent"`, omitted
    otherwise (unchanged default behavior). `NotifyTool` also exposes
    `priority` as a proper LLM-facing parameter (an `enum` of the five
    ntfy levels) -- unlike `actions` (43), this one *is* something worth
    letting the model choose directly when it calls `notify` itself, not
    just an internal/programmatic concern.

    Verified: full suite (305 passed, up from 303) including 2 new
    `tests/test_notify.py` tests (`priority` included when given, omitted
    when not) and an updated `tests/test_proactive.py` assertion proving
    the correlated-urgent-warning test's notification actually carries
    `priority: "urgent"`. Live-verified against a real `uvicorn` process,
    a real mock ntfy server, and a real mock Docker Engine API: reported
    a container as `exited` (a real `critical`/`urgent` classification,
    not a fake), confirmed the notification the mock ntfy server actually
    received carried `"priority": "urgent"` in its JSON body; then
    reported the container healthy again and confirmed a routine
    "notify"-tier (and a fully healthy, no-notification-at-all) proactive
    check both still behave exactly as they did before this milestone.

45. **Done** -- Persona/voice consistency layer. A configurable
    tone/personality profile applied consistently via the system message
    Cortex (33) already builds -- config, not a new subsystem. New
    `SIRISAI_PERSONA`, prepended as a system message on every turn
    Cortex drives (`process()`, `confirm()`, and the planner loop both
    feed through), ahead of context (16) -- "who you are" before "here's
    what's currently relevant," a deliberately separate, always-applied
    concern independent of `use_context`.

    Empty (default), not a built-in opinionated persona: every other
    optional feature in this codebase follows "empty = not engaged"
    (weather, calendar, Docker, notify, Hermes, presence, ...), and this
    milestone follows the same convention rather than being the one
    exception -- baking in a specific tone by default would be imposing
    a choice that's genuinely SirisAI's owner's to make, not
    this codebase's. `.env.example` carries a commented-out starting
    example rather than a live default.

    Verified: full suite (310 passed, up from 305) including 4 new
    `tests/test_cortex.py` tests (prepended when configured, omitted
    when not -- the exact regression check that matters here, since
    every other existing test relies on persona staying off by default
    -- persona ordered before context when both apply, and the
    follow-up call after a confirmed tool also carries it) and one new
    endpoint-level `tests/test_persona.py` test proving it reaches the
    model over the real `POST /siris/chat` route. Live-verified against
    a real `uvicorn` process and a real mock Ollama server logging every
    raw request body it received: a configured persona showed up as the
    first message in the real HTTP payload with `use_context: false`,
    and with context also enabled, the real payload carried both system
    messages in the correct order -- persona first, context second.

46. **Done** -- Multi-user recognition. The biggest, most invasive entry
    in this batch: memory and goals were global before this, with no
    concept of *whose* preference "I like my coffee black" is. This
    entry's own open question -- how a "user" is even identified with no
    camera/biometric input -- was put to the user rather than decided
    silently (same posture as milestone 44's escalation channel choice);
    the answer was a client-supplied user id header (`X-Siris-User`),
    over a per-user API key or a voice-satellite-only concept, for the
    lowest blast radius and uniform behavior across text, voice, and any
    future client -- absent the header, everything is the single
    `"default"` user, so an unconfigured deployment behaves exactly as
    before this milestone.

    The header alone doesn't get you multi-user support, though:
    `SirisAI` and every tool/skill are process-wide singletons
    (`app/core/siris.py`), built once at startup with no per-request
    injection today -- threading a real `user_id` parameter through
    would touch `Tool.run`/`Skill.run`'s base signatures and every
    subclass in `app/plugins/builtin/` and `app/skills/builtin/`, for
    one column. Used a request-scoped contextvar instead
    (`app/core/user_context.py`): a new `resolve_user_id` dependency,
    wired into every authenticated router alongside `require_api_key`,
    sets it from the header; `MemoryStore`/`GoalStore` methods
    (`app/memory/service.py`, `app/goals/service.py`) default their own
    `user_id` keyword to `get_current_user_id()` when a caller doesn't
    pass one explicitly. Every existing route, tool, and skill that
    already just calls `siris.memory.remember(...)` or
    `siris.goals.list_goals(...)` -- including `ContextEngine.build()`,
    `daily_briefing`, and `goal_check_in` -- picked up correct per-user
    scoping with zero call-site changes.

    Cross-user goal mutation (`update_goal_status`/`add_objective`/
    `complete_objective` on a `goal_id` you don't own) is treated
    identically to an unknown id -- `None`, the same 502 an actually
    unknown id gets -- rather than a 403, so a caller can't use the
    error to enumerate which ids exist for someone else.

    Scope cuts, deliberate: entity facts/relationships and events
    (`EntityMemoryStore`/`EventMemoryStore`) and conversations stay
    global -- the roadmap entry itself only named "memory, goals," and
    the world model (what Plex runs on, what depends on what) is
    arguably infrastructure knowledge that *shouldn't* be siloed per
    user anyway. Permission tiers, also named in the original framing,
    stay untouched too -- `ToolPermission` is orthogonal to whose data a
    call touches, a separate axis this milestone didn't need to move.

    This codebase had no Alembic (`app/db/base.py`'s docstring: "add
    migrations before the first model lands") and relied entirely on
    `Base.metadata.create_all()`, which creates missing *tables* but
    never adds a missing *column* to a table that already exists on
    disk -- so a real deployment's existing `memories`/`goals` tables
    needed `user_id` added by hand or every query against them would
    start erroring outright. Added the narrowest thing that fixes it
    (`app/db/migrate.py`): a short, explicit list of (table, column,
    DDL) patches, applied via `PRAGMA table_info` + `ALTER TABLE ADD
    COLUMN` right after `create_all()` -- not a generic schema-diffing
    engine, since there's only ever been this one case so far.

    Verified: full suite (326 passed, up from 310) including 16 new
    `tests/test_multi_user.py` tests -- `resolve_user_id`'s header
    parsing, `MemoryStore`/`GoalStore` isolation at the unit level
    (explicit `user_id`), the cross-user "not found" behavior for every
    goal-mutating method, `stale_goals()` scoping, three migration tests
    against a hand-built pre-milestone-46 SQLite schema (missing column
    added and backfilled, idempotent on a second call, a no-op against
    an empty DB), and endpoint-level isolation over the real
    `X-Siris-User` header for both memory and goals. Live-verified
    against a real `uvicorn` process started against a hand-built SQLite
    file mimicking a genuine pre-milestone-46 database (tables present,
    `user_id` column absent, one pre-existing row in each): confirmed
    the column got added and the old row backfilled to `"default"` on
    startup, confirmed memories/goals created as `alice`/`bob` over real
    HTTP were invisible to each other and to the no-header request, and
    confirmed `bob` attempting `update_goal_status` on `alice`'s real
    goal id got the same 502 "unknown goal_id" an actually-nonexistent
    id gets.

47. **Done** -- Cost/resource dashboard. LLM token spend and homelab
    power draw tracked over time as their own metrics -- direct reuse of
    `MetricsStore`'s existing sample/baseline machinery (27), not a new
    store -- surfaced via `GET /siris/metrics/dashboard`. Ties back to
    milestone 23's own "reduce homelab power consumption" goal example.

    Token spend: Ollama's non-streamed `/api/chat` response already
    reports `prompt_eval_count`/`eval_count`, now parsed into
    `LLMResponse.prompt_tokens`/`completion_tokens` (`app/llm/ollama.py`)
    -- None, not zero, when a backend doesn't report them, so "no usage
    data" stays distinguishable from "used zero tokens". Recording it
    at every one of Cortex's and the planner's several `.generate()`
    call sites would've meant touching both repeatedly for one
    cross-cutting concern, so instead a new `MeteredLLMClient`
    (`app/llm/metering.py`) wraps whatever `build_llm_client()` returns
    in `SirisAI.__post_init__`, transparently to Cortex/Planner -- they
    keep calling `.generate()` on what they still believe is a plain
    `LLMClient`, and every call's usage, when reported, gets recorded as
    three metric samples (`llm_tokens_prompt`/`_completion`/`_total`)
    with zero changes to either caller. Exactly the kind of thing
    `app/llm/base.py`'s "the rest of the app should only ever talk to
    this interface" boundary exists for.

    Power draw: this codebase has no power sensor of its own, so rather
    than inventing one, `SIRISAI_POWER_SENSOR_ENTITY_ID` points at a
    Home Assistant entity (e.g. a smart plug/PDU sensor) and
    `ServerHealthSkill` reads its state as a wattage reading every time
    a health check runs (a chat turn, or `POST /siris/proactive/check`
    on cron) -- same accumulation pattern as its existing cpu/memory/disk
    samples, and the same best-effort external-call wrapping as
    milestone 39's weather/calendar (an unreachable Home Assistant or a
    non-numeric sensor state -- Home Assistant's own "unavailable" -- is
    skipped, not a failed health check). Requires Home Assistant to
    already be configured; empty (default) = power draw isn't tracked.

    `MetricsStore` gained `total()`/`sample_count()`/`latest_value()`
    for this: `total()` suits a counter-shaped metric like token usage
    (sum toward cumulative spend) where `baseline()`'s recent-window
    mean/stdev suits a gauge-shaped one like power draw or cpu_percent
    instead. The dashboard response omits `power_draw` entirely (not a
    zeroed-out object) when the sensor isn't configured -- distinct from
    "configured but no readings yet" -- and stays server-wide rather
    than per-user (milestone 46): infrastructure/cost data, not personal
    preference, same reasoning as leaving the world model global there.

    Verified: full suite (343 passed, up from 326) including 17 new
    `tests/test_cost_dashboard.py` tests -- `MetricsStore`'s three new
    methods, `OllamaClient` parsing real and absent token-usage response
    shapes, `MeteredLLMClient` recording (and correctly *not*
    half-recording a total when only one side is known), power-draw
    recording gated on both settings and Home Assistant being present,
    the best-effort non-numeric-state case, and the dashboard endpoint's
    shape both with and without a configured power sensor. Live-verified
    against a real `uvicorn` process talking to a real mock Ollama
    server (returning genuine `prompt_eval_count`/`eval_count`) and a
    real mock Home Assistant server (serving a fake `sensor.pdu_watts`
    state): a real `POST /siris/chat` turn produced the exact token
    counts the mock returned on the dashboard afterward, and a real
    `server_health_check` tool run produced the exact wattage (212.7)
    the mock's sensor reported.

48. **Done** -- Room/presence-aware routing. A response follows you --
    replies through whichever room's satellite you're actually in
    rather than always the device a request started on. This entry's
    own flagged question -- scope and verification approach, given no
    real satellite hardware is deployed anywhere reachable -- was put
    to the user rather than guessed at: full push-routing, verified
    against real mock satellite servers standing in for hardware (the
    same "real mock HTTP server, real running process" verification
    used for every external integration all through this batch), not a
    narrower room-tracking-only cut and not skipped.

    The satellite (satellite/siris_satellite/) was pull-only before
    this: it calls SirisAI, gets a reply, plays it locally -- SirisAI
    had no way to reach a *different* satellite than the one that
    asked. Now: a satellite with `SIRISAI_SATELLITE_ROOM` and
    `SIRISAI_SATELLITE_CALLBACK_URL` configured registers itself
    (`POST /siris/satellites/register`, a new `SatelliteStore`) on
    startup and opens a small inbound listener (`inbound.py`, plain
    `http.server` in a background thread -- this only ever needs to
    handle occasional single-shot pushes, not a full web framework).
    Every real chat turn a satellite drives also carries its `room` on
    `POST /siris/chat`, which counts as fresh activity there --
    `SatelliteStore.current_room()` is just "whichever room was most
    recently active," no UniFi-presence inference attempted (an honest
    narrowing of the original "UniFi presence, or the satellite you
    last spoke to" framing: the "or" already made either signal
    legitimate on its own, and this codebase's UniFi integration has no
    existing AP-to-room mapping to build the other one from -- a
    separate feature, not this milestone's job to invent). A new tool,
    `speak_in_current_room`, resolves the current room, synthesizes the
    message via TTS, and pushes the audio to that room's satellite --
    registered whenever a TTS backend is configured, no-ops
    (`sent: false`) gracefully when no satellite is currently known.

    `Tool.run()` is a synchronous interface (`HomeAssistantClient`'s own
    docstring: SirisAI's tools never bridge async/sync just to reuse an
    async client), so `speak_in_current_room` couldn't just `await
    siris.tts.synthesize(...)` -- `app/voice/sync_tts.py` mirrors
    `OpenAICompatibleTTSClient`'s exact request shape synchronously
    instead, same reasoning as `HomeAssistantClient` staying sync.
    Empty room/callback config (default) = a satellite behaves exactly
    as before this milestone: pull-only, no registration, no listener.
    No staleness cutoff on `current_room()` either -- a lone satellite
    that registered once, days ago, still counts as current; that's
    future refinement, not guessed at here.

    Verified: full suite (357 passed, up from 343) including 14 new
    `tests/test_satellites.py` tests (`SatelliteStore`'s registry/
    upsert/current-room logic, the registration endpoints,
    `speak_in_current_room` composing correctly and propagating a push
    failure, registration gated on TTS being configured, and
    `POST /siris/chat`'s `room` actually updating `current_room()`) and
    7 new `satellite/tests/` tests (`register()`/room-carrying `chat()`
    on the client, and `inbound.py`'s real-socket listener -- including
    a timing test proving the response returns before slow playback
    finishes, not after). Live-verified against a real `uvicorn`
    SirisAI process, a real mock Ollama server, a real mock
    OpenAI-compatible TTS server, and -- the strongest form of this --
    the actual `siris_satellite.inbound.InboundServer` class from the
    real satellite package (not a reimplementation) bound to two real
    ports standing in for a kitchen and an office satellite: registered
    both, drove a real chat turn with `room="kitchen"`, pushed a reply
    via `speak_in_current_room` and confirmed the kitchen listener
    played the exact audio bytes the real TTS server returned; then
    drove a real chat turn with `room="office"` and confirmed a second
    push landed only on the office listener -- the kitchen listener
    received nothing -- proving the reply actually followed the user
    between rooms, not just that push delivery works to one fixed
    target.

Milestone 48 closed out the third batch (39-48). Development since has been
checked against a written vision document ("Siris AI -- Current Vision &
Development Direction") rather than another brainstorm -- worth recording
the comparison itself, not just what it produced. Its core philosophy,
"SIRIS THINKS. TOOLS DO." -- orchestrate existing specialist systems
(Hermes, Home Assistant, Plex, ...) rather than rebuild their functionality
-- turns out to already describe this codebase closely: every integration
here is a thin client wrapped as a tool, Hermes was already added as
exactly the delegate-executor the document describes (34), Home Assistant
tools stay generic rather than reimplementing automation logic, and
proactive notification stays deterministic and restrained (26, 44) rather
than fuzzy autonomous judgment -- convergent design, not coincidence, since
both this codebase's own conventions and the document independently landed
on "don't rebuild what already works." Four gaps were identified and named
rather than silently patched over: no capability-to-provider indirection
layer (the document's own explicit design -- `capability: diagnose_server,
provider: Hermes` -- swappable without touching SirisAI's architecture;
today a tool like `hermes_run_task` *is* the capability directly, no
abstraction between them); no way to explain a Home Assistant-native event
SirisAI didn't itself trigger (closed by 49, below); no multi-signal
room/person presence with confidence estimates (mmWave, BLE -- needs sensor
hardware not present in this homelab, an honest gap rather than one to
fake); and no physical status display (the document defers this one too).
The capability-indirection gap was deliberately not acted on here --
designing that abstraction now, with exactly one provider per capability,
risks the premature-abstraction the document's own "do not attempt the
entire vision immediately" caution warns against; revisit once a second
provider for the same capability actually exists to design against.

49. **Done** -- Home Assistant logbook ("why did X happen"). Direct
    response to the vision document's own named example -- "why did the
    hallway light turn on?" -- which `explain_recent_action` (28)
    couldn't answer: that tool explains SirisAI's *own* tool calls
    (trigger/evidence/policy from the audit log), not a Home
    Assistant-native automation or state change SirisAI never touched.
    A new `home_assistant_get_logbook` tool (`HomeAssistantClient.get_logbook()`,
    wrapping Home Assistant's own `/api/logbook` REST endpoint) closes
    that gap directly: Home Assistant's logbook already carries
    human-readable "what happened and why" -- including which
    automation triggered a change, where Home Assistant knows one --
    so this is a thin passthrough, not a new explanation engine.
    `entity_id` (optional, e.g. `light.hallway`) filters to one
    entity's history; `hours` (default 24) how far back. Registered
    alongside the other three Home Assistant tools, same
    `SIRISAI_HOME_ASSISTANT_BASE_URL`/`TOKEN` gate, no new setting.

    Verified: full suite (361 passed, up from 357) including 4 new
    `tests/test_home_assistant.py` tests (the client's timestamped-path
    construction and entity-filter query param, the bare-path/no-filter
    case, and the tool's default-24-hours and entity/hours-forwarding
    behavior) plus the existing Home Assistant registration test
    extended to cover the new tool name. Live-verified against a real
    `uvicorn` process and a real mock Home Assistant server seeded with
    a realistic logbook entry ("turned on triggered by automation
    'Motion in hallway after dark'"): a whole-home logbook call
    returned both seeded entries, and filtering to `light.hallway`
    reproduced the vision document's exact example -- returning only
    the hallway entry, with the automation that actually caused it
    right there in the response, not just the light's current state.

50. **Done** -- Media stack + notes integrations: Tautulli, Radarr,
    Sonarr, Bazarr, Listenarr, Cleanuparr, Obsidian. Direct user
    request, seven services at once -- same batching precedent as
    milestone 13's five unrelated integrations in one milestone,
    since these all follow the exact same thin-client-plus-tools shape
    every prior integration here does, not a new mechanism.

    Confidence varies deliberately across the seven, and each is built
    (and documented, and tested) to match:

    - **Tautulli** (`app/tautulli/`) -- Plex activity/history, a
      companion to Plex (13) rather than a replacement. One query
      endpoint (`/api/v2?apikey=&cmd=`) unlike every header-auth'd
      integration here -- Tautulli's own shape, not a deviation from
      this codebase's convention. `tautulli_current_activity`,
      `tautulli_recent_history`, both READ.
    - **Radarr**/**Sonarr** (`app/radarr/`, `app/sonarr/`) -- movie/TV
      collection managers, near-identical Servarr-family v3 APIs
      (X-Api-Key header, `/api/v3/*`). `radarr_list_movies`/
      `sonarr_list_series` and `radarr_queue`/`sonarr_queue` are READ;
      `radarr_search_movie`/`sonarr_search_series` trigger a background
      search (`POST /api/v3/command`) and are SAFE_ACTION -- routine
      and reversible, it only queues a search, same tier as
      `docker_restart_container`. Deliberately stops there: adding new
      movies/series or deleting existing ones isn't built -- a bigger,
      more consequential action (quality profile choice, disk space)
      that deserves its own careful design later, not a side effect of
      "add Radarr support."
    - **Bazarr** (`app/bazarr/`) -- subtitle manager for Radarr/Sonarr
      libraries. `bazarr_wanted_movies`/`bazarr_wanted_episodes`, both
      READ (what's missing subtitles).
    - **Obsidian** (`app/obsidian/`) -- not Obsidian itself, which has
      no API of its own, but the community "Local REST API" plugin
      (the de facto standard way to expose one): HTTPS with a
      self-signed certificate by default, Bearer-token auth.
      `obsidian_list_notes`/`get_note`/`search` are READ;
      `obsidian_create_or_update_note` is **CONFIRM_REQUIRED**, not
      SAFE_ACTION -- unlike a routine restart or a queued search, a PUT
      fully replaces a note's existing content with no built-in undo,
      the same "an unbounded write shouldn't run without a human
      approving it" reasoning as `home_assistant_call_service`'s tier.
    - **Listenarr** (`app/listenarr/`) -- an audiobook/podcast manager.
      **The least confidently verified integration in this codebase,
      more so than UniFi** (13's previous holder of that distinction):
      built from the Servarr family's shared API convention, but with
      no live instance and no solid reference documentation available
      to confirm Listenarr's own resource model against -- what a
      library item is even called. Deliberately limited to
      `listenarr_system_status`/`listenarr_queue`, the two endpoints
      near-universal across every Servarr-family app regardless of
      what they manage, rather than guessing at a library-listing
      endpoint's resource name and shipping something that looks more
      verified than it is.
    - **Cleanuparr** (`app/cleanuparr/`) -- stalled/failed download
      cleanup for *arr apps and download clients. **Even less
      confidently verified than Listenarr**: no solid reference
      documentation for its API was available at all, so this is
      limited to `cleanuparr_status`, a single health-check-style call
      against a path convention common to ASP.NET Core apps (its
      apparent stack). `POST /siris/integrations` (32's OpenAPI
      discovery) is the recommended path for real Cleanuparr coverage,
      once it can be pointed at a live instance or spec, rather than
      more hand-guessed endpoints here.

    All seven follow the standing "empty = tool(s) not registered"
    convention -- each needs its own `base_url`+`api_key` pair set (see
    `.env.example`) before its tools appear at all.

    Verified: full suite (389 passed, up from 361) including 28 new
    tests across seven new test files (`tests/test_tautulli.py`,
    `test_radarr.py`, `test_sonarr.py`, `test_bazarr.py`,
    `test_listenarr.py`, `test_cleanuparr.py`, `test_obsidian.py`) plus
    registration-gating tests added to
    `tests/test_tool_library_registration.py` for all seven. Live-
    verified against a real `uvicorn` process and five real mock HTTP
    servers standing in for Tautulli/Radarr/Sonarr/Bazarr/Obsidian,
    each asserting the real request shape it received (API key
    header/param, exact command payloads for Radarr/Sonarr's search
    triggers): every tool call round-tripped correctly, including
    Obsidian's `CONFIRM_REQUIRED` write being blocked without
    confirmation and then actually writing once confirmed. Listenarr
    and Cleanuparr were also live-verified against their own real mock
    servers -- confirming the plumbing (registration, auth header,
    response parsing) works end to end -- but, per the confidence
    caveats above, the *endpoints themselves* still need checking
    against real instances before they can be trusted the way the other
    five can.

51. **Done** -- Configurable confirmation bypass for Home Assistant
    device control. Direct user request: routine calls like "turn on
    the kitchen light" were stopping for confirmation every time, since
    `home_assistant_call_service` (9) is `CONFIRM_REQUIRED` -- correctly
    so in general, since it's unbounded (it can call *any* Home
    Assistant service, including locks and alarms), but that same
    blanket rule was also gating routine lighting/switch control.

    Two ways to relax this were on the table -- remove confirmation
    entirely, or allowlist specific safe domains -- and this was put to
    the user rather than picked silently, since it's a real safety
    tradeoff the tool's own original design comment explicitly
    considered and rejected ("confirm before running rather than trying
    to allowlist safe services"). Answer: the allowlist. New
    `Tool.needs_confirmation(arguments) -> bool` hook (`app/plugins/base.py`,
    same "optional, sensible default, override only where a real gap
    exists" shape as `dry_run()`/`verify()`) -- `True` by default, so
    every other `CONFIRM_REQUIRED` tool (e.g. `obsidian_create_or_update_note`,
    50) is completely unaffected. `SirisAI.run_tool()` now consults it
    (only when `permission` is `CONFIRM_REQUIRED`) before deciding
    whether to raise `ConfirmationRequired`, instead of trusting the
    tool's static permission tier alone.

    `HomeAssistantCallServiceTool.needs_confirmation()` is the one
    override: a new `SIRISAI_HOME_ASSISTANT_NO_CONFIRM_DOMAINS`
    (comma-separated, e.g. `light,switch,fan`) lets specific domains
    skip confirmation and run immediately -- everything not listed
    (locks, alarms, covers/garage doors, climate, ...) still stops for
    approval exactly as before. Empty (default) = every call still
    requires confirmation, unchanged from before this milestone.

    The audit log's `policy` field (28) gained a third
    `CONFIRM_REQUIRED` message ("this call's arguments are allowlisted
    to skip confirmation"), distinct from "explicitly confirmed" -- a
    call that never needed asking shouldn't be indistinguishable, in
    `explain_recent_action`'s eyes, from one a human actually approved.

    Verified: full suite (402 passed, up from 393) including 6 new
    `tests/test_permissions.py` tests (a generic fake `CONFIRM_REQUIRED`
    tool overriding `needs_confirmation()` -- allowlisted calls run and
    record the new policy string, non-allowlisted calls still block and
    still support explicit confirmation) and 9 new
    `tests/test_home_assistant.py` tests (the tool's own allowlist
    logic, settings parsing, and two end-to-end `SirisAI.run_tool()`
    tests -- via `unittest.mock.patch.object` on the registered client,
    not raw HTTP mocking, since the point was proving the *setting*
    actually flows through registration into the live tool instance).
    Live-verified against a real `uvicorn` process and a real mock Home
    Assistant server with `SIRISAI_HOME_ASSISTANT_NO_CONFIRM_DOMAINS=light,switch,fan`:
    a real `light.turn_on` call ran immediately with the new "allowlisted"
    policy string in the audit log, a real `lock.unlock` call on the
    same instance was still blocked pending confirmation, and confirming
    it afterward ran and recorded the original "explicitly confirmed"
    policy string, unchanged.

52. **Done** -- Performance hardening pass. Direct user request: "speed
    up the system and speed up the responses from SirisAI and tool
    queries." Four findings, all fixed:

    **Event-loop blocking (the big one).** This process runs as a single
    `uvicorn` worker with one asyncio event loop (see the Dockerfile --
    no `--workers` flag). `Cortex._run_single_tool()` and `Planner.run()`
    are both `async def`, but were calling `SirisAI.run_tool()` --
    synchronous, and free to do arbitrary blocking HTTP I/O via any
    tool's own client (Home Assistant, Docker, Radarr, ...) -- directly,
    not off-loaded to a thread. A single slow tool call therefore froze
    the *entire* process: every other concurrent request (another chat
    turn, a voice satellite, a proactive check) stalled for that call's
    full duration regardless of what it was doing. `ContextEngine.build()`
    -- called on nearly every chat turn, and itself doing blocking work
    including a mandatory 200ms `psutil.cpu_percent(interval=0.2)` sleep
    plus DB queries -- had the same problem, called directly from
    `Cortex.process()`. Fixed by routing both through `asyncio.to_thread()`
    instead of calling them directly, so the blocking work happens on a
    worker thread and the event loop stays free. (Sync route handlers
    like `app/api/routes/proactive.py`/`routines.py` and
    `POST /siris/tools/{name}/run` were checked too -- already plain
    `def`, which FastAPI/Starlette auto-threadpools, so no change needed
    there.)

    **N+1 queries in `GoalStore`.** `list_goals()`/`stale_goals()` fetched
    goal ids, then called a per-goal serializer that ran two more queries
    each -- 1 + 2N queries for N goals. New `_serialize_goals(session, goals)`
    batches every goal's objectives into one `goal_id IN (...)` query, so
    both methods now run 2 queries total regardless of N.
    `_serialize_goal(session, goal_id)` (the old per-row path) stays, for
    the single-goal call sites (`create_goal`/`update_goal_status`/
    `add_objective`/`complete_objective`) where there's no N to eliminate.
    Grepped the other stores (`EntityMemoryStore`, `EventMemoryStore`,
    `RoutineStore`, ...) for the same pattern -- none had it.

    **No SQLite WAL mode.** The default rollback-journal mode serializes
    every reader against every writer, which didn't matter much when
    everything ran on one thread -- but the event-loop fix above means
    tool calls, context building, and audit-log writes now genuinely run
    concurrently across threads. `app/db/session.py` sets
    `PRAGMA journal_mode=WAL` and `PRAGMA synchronous=NORMAL` on every
    new connection (readers and a writer can now proceed concurrently;
    `synchronous=NORMAL` skips an fsync per commit -- durable against an
    app crash, not an OS crash/power loss, an accepted tradeoff for a
    homelab personal assistant, not a financial ledger).

    **Missing indexes.** Grepped every `created_at` column in
    `app/db/models.py` -- none had `index=True`, despite being the
    primary `ORDER BY` column for `AuditLogStore.list_recent()`/
    `find_recent()` (the highest-write-volume table), `MemoryStore.recall()`/
    `list_all()`, `EventMemoryStore.recent_events()`, and
    `GoalStore.list_goals()`. Added `index=True` to all four. (Not
    `Goal.updated_at`, also read by `stale_goals()` -- that query already
    filters to `status="active"` first, a small subset, so the extra
    index wouldn't earn its upkeep cost.)

    Both the index and the WAL settings need to reach databases that
    already exist on disk, not just fresh installs -- `create_all()`
    only creates missing tables. `app/db/migrate.py` gained
    `apply_index_patches()` (same hand-rolled-patch-list shape as
    `apply_column_patches()`, milestone 46) with `CREATE INDEX IF NOT
    EXISTS` for each of the four columns above, using SQLAlchemy's own
    default index-naming convention (`ix_<table>_<column>`) so it's a
    true no-op on a fresh install; WAL/`synchronous` are connection
    pragmas, not schema, so they apply automatically to any existing DB
    file the moment it's opened -- no migration needed.

    Verified: full suite (411 passed, up from 402) including two new
    `asyncio.gather`-based concurrency tests (`tests/test_cortex.py`,
    `tests/test_planner.py`) that register a tool/context engine that
    sleeps 0.3s, run two concurrent `Cortex.process()` calls, and assert
    the pair finishes in well under the 0.6s serialized execution would
    take; two new query-count tests (`tests/test_goals.py`) using a
    `sqlalchemy.event` listener on `before_cursor_execute` to prove
    `list_goals()`'s query count stays constant as goal count grows, plus
    a grouping test guarding the objective-batching logic against
    cross-goal mixups; and three new migration tests plus a WAL-mode
    test (`tests/test_multi_user.py`), mirroring the existing
    `apply_column_patches()` tests. Each fix was verified to actually
    catch its regression, not just pass tautologically: the planner
    concurrency test was confirmed to fail (0.607s, i.e. serialized) when
    temporarily reverted to a direct `run_tool()` call, then pass again
    restored; the `GoalStore` query-count test was confirmed to fail
    (13 queries scaling to 23 as more goals were added) against a
    temporarily-reverted N+1 `list_goals()`, then pass again restored.
    Live-verified against a real `uvicorn` lifespan and a real SQLite
    file: `PRAGMA journal_mode` reads back `wal`, and
    `sqlite_master` shows `ix_goals_created_at` present after startup.

53. **Done** -- Richer, voice-delivered daily briefing. Direct follow-up
    to a "what else fits the Jarvis concept" brainstorm: the cheapest,
    highest-value idea on the table was composing what already exists
    (`DailyBriefingSkill`, milestone 39, and `speak_in_current_room`,
    milestone 48) rather than building anything new -- no new
    integration, no new subsystem.

    `DailyBriefingSkill` gained recent Plex activity via
    `tautulli_recent_history`, registered only when Tautulli is
    configured, same "enrichment, not the point -- an unreachable
    backend shouldn't take the whole briefing down" try/except precedent
    as weather and calendar. The original brainstorm framing was
    "overnight activity," but Tautulli's `date` field is never parsed
    into a real datetime anywhere in this codebase (app/tautulli/client.py
    passes it through as-is) -- filtering to an "overnight" window would
    mean inventing a time-parsing rule this milestone can't verify
    against a real instance, so it surfaces the last few plays as-is
    instead, the same honest-about-what-the-data-supports call milestone
    39 already made for "goals due soon" (no due-date field) vs. "active
    goals."

    `POST /siris/proactive/briefing` now also speaks the summary aloud
    via `speak_in_current_room`, alongside (not instead of) the existing
    push notification -- two independent delivery channels, each fires
    only if configured/currently reachable. `BriefingResponse` gained a
    `spoken` field reporting whether it actually went out loud, not just
    whether speaking was attempted: `speak_in_current_room` returns
    `sent: false` (not an error) when no satellite is currently known --
    e.g. nobody's home to hear it -- and the endpoint has to report that
    honestly rather than claiming success.

    Verified: full suite (418 passed, up from 411) covering the skill's
    new Tautulli inclusion (present when configured, absent and
    non-fatal when unreachable or unconfigured, correctly reports "Nothing
    recently watched" when history is empty) and the endpoint's new
    speak path (spoken: true when a satellite answers, spoken: false and
    honestly reported -- not an error -- when none is known, no call
    attempted at all when no TTS backend is configured). Live-verified
    against a real `uvicorn` process, a real mock TTS server, a real mock
    Tautulli server, and a real mock satellite callback server: a real
    `speak_in_current_room` call round-tripped actual synthesized audio
    bytes from the mock TTS server through to the mock satellite's
    `/play` endpoint (confirmed by byte count, not just a 200 OK), and
    `daily_briefing`'s summary correctly included "Recently watched: The
    Matrix (brad); Inception (brad)" sourced from the mock Tautulli
    server's real HTTP response.

54. **Done** -- Home Assistant automation authoring. Another pick from
    the same "what else fits the Jarvis concept" brainstorm: until now
    `home_assistant_call_service` (9) can only *call* an existing
    service once -- it can't create the standing rule ("if motion and
    it's dark, turn on the hallway light") that today has to be built by
    hand in Home Assistant's own UI. New `home_assistant_create_automation`
    tool closes that gap.

    Home Assistant's officially documented REST API
    (https://developers.home-assistant.io/docs/api/rest/) has no
    endpoint to persist a new automation -- there's no public equivalent
    of `POST /api/services/<domain>/<service>` for "create this
    automation." `HomeAssistantClient.create_automation()` uses
    `POST /api/config/automation/config/<id>` instead: the same
    config-editor endpoint Home Assistant's own frontend calls when you
    edit an automation in the UI, takes the same bearer-token auth as
    every other call this client makes, and is widely used by community
    tooling -- but it isn't part of the documented public surface the
    way `list_states`/`call_service`/`get_logbook` are. Flagging that
    explicitly rather than presenting it with the same confidence as the
    rest of this client, same "confidence-tiered" precedent milestone
    50 set for Listenarr/Cleanuparr: this should be confirmed against a
    real instance, not just this milestone's mock server, before relying
    on it.

    `trigger`/`condition`/`action` are passed through to Home Assistant
    as-is rather than re-encoded as a rigid tool parameter schema --
    that shape is extensively documented, public YAML an LLM already
    knows from training data, and hand-modeling it here risks this
    codebase's own copy drifting from Home Assistant's, or omitting a
    trigger/condition/action type this codebase's author never thought
    of. Home Assistant's own config validation is still the real
    backstop against a malformed payload either way. The automation's id
    is a fresh millisecond timestamp -- the same convention Home
    Assistant's own UI uses for a new automation -- generated fresh
    per call so a collision (and silently overwriting an unrelated
    automation) isn't realistically going to happen.

    Discovering automations that already exist doesn't need a new tool
    at all: `home_assistant_list_entities` with `domain='automation'`
    already lists them (automations are entities), so this milestone is
    purely additive.

    Standing rules are a different order of consequence than one
    `call_service` invocation -- "turn on the kitchen light once" being
    allowlisted (51) says nothing about whether "always turn on the
    kitchen light when motion is detected, forever, until someone
    notices and removes it" should skip a human looking at it first. So
    `home_assistant_create_automation` is its own `Tool` class with no
    `needs_confirmation()` override, unconditionally `CONFIRM_REQUIRED`
    regardless of `SIRISAI_HOME_ASSISTANT_NO_CONFIRM_DOMAINS` -- that
    allowlist only ever applies to `home_assistant_call_service`.

    Verified: full suite (423 passed, up from 418) covering the request
    payload (required fields, and that `condition`/`mode`/`description`
    are included only when given), the generated id's shape, and that
    confirmation is required unconditionally end-to-end (blocked
    unconfirmed, runs and returns the generated `automation_id` once
    confirmed) via `siris.run_tool()`. Live-verified against a real
    `uvicorn` process and a real mock Home Assistant config-API server:
    an unconfirmed call correctly returned `confirmation_required`
    instead of running, and confirming it sent the exact expected
    `trigger`/`action`/`condition` payload to
    `POST /api/config/automation/config/<generated-id>` on the mock
    server -- confirmed by inspecting what the mock server actually
    received, not just a 200 OK.

55. **Done** -- Unrecognized network device alert. The last pick from
    the same brainstorm, with a real detour: the original plan was to
    build this on `unifi_list_clients`, but reading `app/unifi/client.py`
    first turned up that it always raises `NotImplementedError` -- a
    parallel session's live test against a real controller found the
    configured API key only authenticates against UniFi's cloud Site
    Manager API, which has no client-listing endpoint at all (only a
    local controller's own Integration API exposes that). Building a new
    feature on a call that's confirmed to always fail wasn't worth doing
    quietly, so this got a mid-flight redesign rather than silently
    inheriting a foundation that was already known-broken -- and it
    surfaces a real, separate problem worth knowing: `presence_check`
    (42) calls the exact same `unifi_list_clients`, so it's likely
    non-functional too, against this same real deployment, independent
    of this milestone.

    Put to the user rather than guessed: build this on Home Assistant's
    `device_tracker` domain instead (a working, already-verified path,
    if HA is tracking devices at all -- via a UniFi HACS integration,
    ping, or anything else), scaffold it anyway against the broken UniFi
    path, or skip it. Answer: Home Assistant.

    New `NewDeviceAlertSkill` (`new_device_alert`) composes
    `home_assistant_list_entities` (domain='device_tracker') with a new
    small store, `KnownDeviceStore` (`app/network_devices/service.py`,
    `known_network_devices` table) -- same "own small purpose-built
    store" shape as `SatelliteStore`/`GoalStore`, global rather than
    per-user (milestone 46) for the same reason `SatelliteStore` is:
    which devices are on the household network is physical
    infrastructure, not conversation content scoped to whoever's
    chatting. Persisted rather than an in-memory set on purpose -- this
    process restarts, and an in-memory set would forget every known
    device on every restart and flag the whole household as "new" the
    next time it ran. The first-ever check (same "baseline, not a guess"
    pattern `PresenceSkill` already established) records what's there
    instead of treating everything as newly arrived; only a device that
    appears *after* a baseline exists gets flagged, and a previously-known
    device that's currently absent (out of range, powered off) is left
    alone -- silently staying in the known set -- rather than treated as
    a departure worth reporting, since that's not what this milestone is
    for.

    Composed into `POST /siris/proactive/check` alongside
    `server_health_check`/`event_timing_check`/`goal_check_in`, same
    NotificationManager (26) pass as the rest -- a new `"network"`
    category classification was added for its warning shape. Deliberately
    never escalates to `critical`/`urgent` the way a `Container 'X' is
    exited` warning can: a guest's phone or a new IoT device is a
    plausible, non-alarming explanation, not confirmed trouble, so
    "notify" (surfaced, not buried, but not breaking through
    do-not-disturb either) is the honest default given that
    false-positive rate.

    Verified: full suite (434 passed, up from 423) covering
    `KnownDeviceStore` directly (an isolated in-memory DB, same pattern
    as `test_satellites.py`), the skill (baseline establishment, a new
    device flagged once a baseline exists, a disappeared device raising
    no warning, graceful no-op when Home Assistant isn't configured), and
    its composition into `POST /siris/proactive/check` (the new field
    present in the response, a `"network"`-category notification
    produced). Two pre-existing test files
    (`tests/test_proactive.py`, `tests/test_explainable_autonomy.py`)
    needed their own fake `siris.run_tool()` stubs extended for the new
    composed call -- caught immediately by the full suite, not shipped
    broken. Live-verified against a real `uvicorn` process and a real
    mock Home Assistant server with a mutable device list: the first
    `POST /siris/proactive/check` established a baseline with zero
    warnings, adding a new `device_tracker` entity to the mock server and
    checking again correctly flagged it (`category: "network"`,
    `action: "notify"`, not `"urgent"`), and a third check with no
    further changes went quiet again -- confirming the full
    baseline -> flag -> steady-state cycle against a real running
    process, not just mocked tool calls.

56. **Done** -- Deeper Radarr/Sonarr/Tautulli tool coverage. Direct
    request: "more tools/skills/actions" for these three, already
    configured and live-verified since milestone 50. Radarr's and
    Sonarr's tool files each carried an explicit note that adding/
    removing library items was deliberately deferred ("a bigger, more
    consequential action... deserves its own careful design later, not
    folded in as a side effect") -- that's the work this milestone does.

    Both gained the same shape, mirroring each other exactly (matching
    the Servarr family's own shared API design): `*_lookup_*` (search
    TMDB/TVDB for something to add -- not a library search),
    `*_list_root_folders` / `*_list_quality_profiles` (discover valid
    ids/paths a caller needs before adding anything), `*_add_*` and
    `*_delete_*` (the actual library changes), and `*_calendar` (upcoming
    release/air dates). `*_add_*`/`*_delete_*` are `CONFIRM_REQUIRED` --
    unlike a routine search, these change what's actually in the library,
    and delete can remove files from disk, so they get the same
    human-in-the-loop treatment as `HomeAssistantCallServiceTool` by
    default rather than `SAFE_ACTION`'s "runs immediately." Everything
    else (lookup/root-folders/quality-profiles/calendar) is `READ`, same
    as the tools already there.

    Sonarr's episode-level monitoring (e.g. "only get season 2") isn't
    built here -- `sonarr_search_series` already searches every monitored
    episode of a series, which covers the common "get me this show" ask;
    per-episode granularity is a separate, smaller feature this milestone
    doesn't need.

    Tautulli gained two: `tautulli_home_stats` (most-watched movies/
    shows, most active users -- Tautulli's own home-page stat categories)
    and `tautulli_terminate_session` (stop a currently-playing stream,
    `CONFIRM_REQUIRED` since it interrupts someone's actual playback, not
    just a query). `tautulli_current_activity` now also returns each
    session's `session_id` -- previously dropped from the mapped output,
    but exactly what `terminate_session` needs to target a specific
    stream.

    Verified: full suite (457 passed, up from 434) covering every new
    client method and tool (request/response mapping, `required`
    parameters, `CONFIRM_REQUIRED` on every add/delete/terminate tool)
    plus end-to-end confirmation-gating tests via `siris.run_tool()`
    (blocked unconfirmed, runs once confirmed) for `radarr_add_movie` and
    `sonarr_add_series`. Live-verified against a real `uvicorn` process
    and real mock Radarr/Sonarr/Tautulli servers: every new READ tool
    (lookup, root folders, quality profiles, calendar, home stats)
    returned real mapped data from real HTTP responses, and every
    `CONFIRM_REQUIRED` tool (`radarr_add_movie`/`delete_movie`,
    `sonarr_add_series`/`delete_series`, `tautulli_terminate_session`)
    was confirmed to block unconfirmed and then, once confirmed, send the
    exact expected payload -- checked by inspecting what each mock server
    actually received, not just a 200 OK.

57. **Done** -- More tool coverage across Home Assistant, UniFi, Hermes
    Agent, Tautulli, Radarr, Sonarr, and Bazarr. Direct follow-up request
    to keep going on all seven. Two of the seven get nothing new, on
    purpose, and that's worth explaining rather than silently skipping:

    - **UniFi**: stays read-only. Its own client docstring, rewritten
      days ago after live-testing against a real controller (milestone
      56's merge), says exactly why: "the least-verified integration in
      this tool library... kept read-only for that reason -- a path
      mismatch fails loudly rather than doing anything destructive."
      Adding a write action (e.g. blocking a client) now would mean
      overriding a deliberate, very recently and carefully verified
      decision without the means to re-verify it against real hardware
      from here. Left alone.
    - **Hermes Agent**: stays a single delegate tool. Its docstring is
      explicit: "one delegate tool, not a growing set of Hermes-specific
      ones, since the actual capability lives on Hermes's side." Adding
      Hermes-specific tools here would contradict that design directly,
      not extend it.

    The other five each gained real, scoped additions:

    **Home Assistant**: `home_assistant_get_history` (a raw state-over-
    time series for one entity -- "how has the temperature changed
    today" -- distinct from `get_logbook`'s discrete named events),
    `home_assistant_render_template` (evaluates a Jinja2 template
    against current state via Home Assistant's own official templating
    endpoint, read-only, for values no single tool returns directly,
    e.g. an average across several sensors), and
    `home_assistant_delete_automation` (removes an automation by its
    config id, `CONFIRM_REQUIRED` and not eligible for the no-confirm
    allowlist, same reasoning as `create_automation`, 54). A
    `list_automations` tool was considered and dropped: `home_assistant_
    list_entities` with `domain='automation'` already covers discovery
    (automations are entities), and an automation's config id -- needed
    to delete it -- turns out to already be exposed as the `id`
    attribute on that same entity's state, so no new lookup method was
    needed either.

    **Radarr and Sonarr**: `*_remove_queue_item`, `CONFIRM_REQUIRED` --
    clearing a stuck or errored download, with an optional blocklist so
    it isn't grabbed again. `queue()`'s mapped output gained the
    item's `id`, previously dropped, since removal needs it.

    **Tautulli**: `tautulli_get_users` (who has access to the server, not
    who's watching right now) and `tautulli_get_libraries` (per-library
    item counts -- distinct from `PlexLibrarySectionsTool`, which only
    lists section names).

    **Bazarr**: `bazarr_search_wanted_subtitles`, `SAFE_ACTION` (same
    "queues a background search" treatment as Radarr's/Sonarr's search
    tools) -- runs Bazarr's own built-in scheduled task by name rather
    than a per-item search: Bazarr's per-item endpoints need a specific
    provider/language selection this codebase doesn't have enough
    verified detail on yet, so this sticks to the one whole-library
    action simple enough to be confident about.

    Verified: full suite (474 passed, up from 459) covering every new
    client method/tool (request/response mapping, `CONFIRM_REQUIRED` on
    every delete/remove tool, `SAFE_ACTION` on the Bazarr trigger).
    Live-verified against a real `uvicorn` process and real mock Home
    Assistant/Radarr/Sonarr/Tautulli/Bazarr servers: every new READ tool
    returned real mapped data from real HTTP responses (including
    `render_template`'s plain-text, non-JSON response), and every
    `CONFIRM_REQUIRED` tool (`home_assistant_delete_automation`,
    `radarr_remove_queue_item`, `sonarr_remove_queue_item`) was confirmed
    to block unconfirmed and then, once confirmed, send the exact
    expected payload -- checked by inspecting what each mock server
    actually received.

58. **Done** -- Removed a 200ms blocking wait from nearly every chat
    turn. Direct follow-up to "I want the responses to be faster,"
    after milestone 52's event-loop-blocking fix: that milestone moved
    blocking tool calls onto a worker thread so they stop freezing
    *other* requests, but the *user's own* turn still waited out every
    blocking call in full -- this milestone hunts for one of those on
    the single most common path, a plain chat turn.

    Found it in `SystemInfoTool` (`app/plugins/builtin/system_info.py`):
    `psutil.cpu_percent(interval=0.2)` deliberately blocks for 200ms to
    measure CPU usage over a fresh window. `ContextEngine.build()` --
    which runs by default on every `POST /siris/chat` call
    (`use_context=True`) -- calls this tool for its "current system
    status" line, so that 200ms landed on essentially every chat
    response, on top of everything else the turn was already doing.
    `server_health_check` (15) hits the same cost on every proactive
    check for the same reason.

    Fixed with psutil's own standard non-blocking pattern, not a
    workaround: `psutil.cpu_percent(interval=None)` returns instantly,
    using the delta since whenever `cpu_percent()` was last called
    anywhere in this process -- so `app/main.py`'s startup lifespan now
    primes it once at boot (so the first real call gets something to
    diff against instead of a meaningless `0.0`), and every call after
    that is free. The tradeoff this accepts: a reading over "however
    long it's been since the last call" instead of a controlled 200ms
    window -- for a tool re-invoked every chat turn and health check
    anyway, that's normally seconds to minutes, arguably more
    informative than a narrow 200ms snapshot, not less.

    Verified: full suite (475 passed, up from 474) including a new
    regression test asserting two consecutive real `SystemInfoTool.run()`
    calls together complete in well under 150ms -- confirmed to actually
    catch the regression by temporarily reverting to `interval=0.2` and
    seeing it fail at ~400ms (two blocking calls), then restoring the fix
    and confirming it passes again, same rigor as milestone 52's
    concurrency tests. Live-verified against a real `uvicorn` process:
    two consecutive real `POST /siris/tools/system_info/run` calls
    completed in 70ms and 12ms respectively (both including real HTTP
    round-trip overhead, not just the bare function call), with the
    second call returning a real non-zero CPU reading (27.8%) -- proving
    the priming call actually works end to end, not just in isolation.

59. **Done** -- Batched LLM token-usage recording into one DB round trip
    off the event loop. Same "keep hunting for hot-path latency" request
    that produced milestone 58, and the same category of miss:
    `MeteredLLMClient.generate()` (47) recorded up to three metrics
    (prompt/completion/total tokens) via three separate synchronous
    `MetricsStore.record_sample()` calls -- three session-open/insert/
    commit round trips -- directly in an `async def` method on the
    response path of *every* chat turn. This predates milestone 52's
    `asyncio.to_thread()` pass (which covered `run_tool()`/
    `ContextEngine.build()`) and was simply missed, not a new problem.

    New `MetricsStore.record_samples(dict[str, float])` writes any number
    of named samples in one session/commit; `MeteredLLMClient.generate()`
    now builds one dict and makes one `asyncio.to_thread()`-wrapped call
    instead of up to three direct ones. `record_sample()` itself is
    untouched -- other call sites (`server_health.py`'s per-metric
    recording) aren't on this hot path and don't need to change.

    Verified: full suite (479 passed, up from 475). A spy-based test
    (`unittest.mock.patch.object`, wrapping the real method rather than
    replacing it) confirms `record_sample()` is never called and
    `record_samples()` is called exactly once with all three values --
    the actual claim being made, not an SQL-statement count (three rows
    still means three INSERT statements whether batched into one
    transaction or not; what changed is the number of sessions/commits,
    not the row count). A concurrency test (same shape as milestone 52's
    Cortex/Planner ones) proves a slow `record_samples()` doesn't block a
    second, concurrent `generate()` call -- confirmed to actually catch
    the regression by temporarily removing the `asyncio.to_thread()` wrap
    and seeing it fail at ~0.6s (serialized), then restoring the fix.
    That test uses the real production `SessionLocal`/engine rather than
    an isolated in-memory one, for a reason worth recording: an isolated
    `StaticPool` engine (this test suite's usual pattern for isolation)
    shares one raw SQLite connection across threads, which isn't safe for
    two genuinely concurrent flushes at once -- confirmed live, it raised
    a SQLAlchemy `FlushError` under this exact test. The real engine's
    connection pool + WAL mode (52) is what actually makes concurrent
    cross-thread writes safe; milestone 52's own concurrency tests used
    the real engine for the same reason, this just hadn't come up again
    until a test finally exercised concurrent writes through it.

60. **Done** -- Strengthened the Hermes Agent connection. Direct
    request, following a question about whether reaching other hosts
    (e.g. a Raspberry Pi) should be Hermes's job or SirisAI's own --
    answer: Hermes's, since it already exists so SirisAI doesn't
    reimplement system-level capabilities as its own tools, and whether
    it can reach another host is a Hermes-side configuration question,
    not something to build here. This milestone strengthens the
    *connection itself*, not what Hermes can be asked to do.

    Two ideas considered and dropped before writing any code, because
    they'd have been solving problems that don't exist: giving
    `hermes_run_task` the SirisAI `conversation_id` for "continuity"
    across delegated calls, and threading planner `evidence` into what's
    sent to Hermes. Neither holds up -- `SirisAI.run_tool()` calls
    `tool.run(**arguments)`, nothing else; no tool anywhere gets
    `conversation_id` or `evidence` at runtime, both are audit-log-only.
    The model choosing to call `hermes_run_task` already sees the full
    conversation (and, mid-plan, every prior step's result) before
    composing the `instruction` argument -- threading either through
    the `Tool` interface just for Hermes would mean special-casing one
    tool's call signature to duplicate context the model can already
    just write into a better-composed instruction. Also considered and
    left alone: whether `hermes_run_task` should be `CONFIRM_REQUIRED`
    rather than `SAFE_ACTION`, given "terminal access" is a wide-open
    capability -- that's a real, deliberate tradeoff already reasoned
    about in this file's own docstring, not a bug, so it's flagged here
    rather than silently relitigated.

    What actually shipped: new `hermes_status` tool (`HermesClient.
    check_status()`, `GET /v1/models` -- the same OpenAI-compatible
    convention `run_task()` already relies on) answers "is Hermes
    actually reachable" and which models it currently reports, without
    spending a real delegated task finding that out the hard way --
    doesn't reopen "one delegate tool," since it's connectivity
    introspection, not a second way to get something *done*. And a new
    `SIRISAI_HERMES_TIMEOUT_SECONDS` setting (default 120, matching the
    existing hardcoded value) -- Hermes may do its own multi-step tool
    use/terminal work before responding, and a fixed ceiling with no
    override risked cutting off a genuinely heavy delegated task with no
    way to raise it.

    Verified: full suite (486 passed, up from 479) covering
    `check_status()`'s request/response mapping and error propagation,
    `hermes_status`'s registration and `READ` permission, and the
    configured timeout actually reaching the real `httpx.Client` both
    directly and through `SirisAI`'s registration path. Live-verified
    against a real `uvicorn` process and a real mock Hermes server:
    `hermes_status` returned the real reachable/model-list response, and
    `hermes_run_task` round-tripped a real instruction through a real
    `POST /v1/chat/completions` call and back.

61. **Done** -- Automatic memory extraction. Direct request: "SirisAI
    should be self-learning and automatically record things to memory
    if they are worth remembering." The `remember`/`remember_entity_fact`
    tools were already callable by the model on its own initiative --
    nothing gated that. The actual gap was structural: a normal chat
    turn allows exactly one tool call, so the model can't both answer
    the user's real request *and* separately save a memory in the same
    turn. Asked the user how this should be decided (an extra pass
    after each turn / a periodic batch review / relying on planner mode
    already being on); they picked the extra pass.

    What shipped: after `Cortex.process()`/`confirm()` produce a real
    response, a separate best-effort pass reviews the exchange and
    decides, using its own judgment, whether anything is worth keeping
    long-term. It runs through `Planner.run()` directly (not
    `Cortex._run_planned()`), so it can check existing memory before
    writing and isn't capped at one call -- but that `Planner.run()`
    call never touches `siris.conversation`, so the extraction pass
    never appears in the visible transcript. It's given only six tool
    specs (`recall`, `remember`, `recall_entity_facts`,
    `remember_entity_fact`, `recall_entity_relationships`,
    `remember_entity_relationship`) via `ToolRegistry.specs(exclude=...)`
    -- deliberately not `log_event`/`recall_events` (events aren't
    durable facts) or anything else (Docker, Home Assistant, Radarr,
    etc.), so even a misbehaving extraction pass can't do anything
    consequential. It's routed through `ModelRouter`'s "fast" role when
    one is configured, since this is a cheap background pass, not the
    user-facing answer. Every write it makes is tagged with an honest
    audit trigger (`"automatic memory extraction: post-turn review (step
    N)"`, via a new `trigger_template` parameter on `Planner.run()`) so
    `explain_recent_action`/`GET /siris/activity` can always tell "the
    user asked for this" apart from "SirisAI decided to remember this
    on its own" -- the same discipline milestone 28 established.
    Failures are swallowed (logged, never surfacing to the turn's own
    response) and it's skipped entirely when the turn itself errored,
    matching the existing best-effort precedent set by
    `ContextEngine.build()`/`server_health_check`. A new
    `SIRISAI_AUTO_MEMORY_EXTRACTION_ENABLED` setting (default `true`)
    turns off the extra LLM call/cost per turn, not the underlying
    tools -- the model can still be asked to remember something
    directly either way.

    Verified: full suite (496 passed, up from 486) covering successful
    end-to-end extraction, default-on/off behavior, that the extraction
    pass never pollutes the visible transcript, that its failures don't
    affect the turn's own response, that it's skipped when the turn
    errored, its honest audit trigger, `confirm()` going through the
    same path as `process()`, fast-model-role routing, and (in
    `test_planner.py`) that `Planner.run()`'s new `trigger_template`
    defaults to the existing per-step string and can be overridden.
    Live-verified against a real `uvicorn` process and a real mock
    Ollama-compatible backend: told it "My favorite color is teal,"
    got back the turn's own unrelated answer with no tool call visible
    to the caller, then confirmed via `GET /siris/memory` that the fact
    was actually saved, `GET /siris/activity` showed the write tagged
    with the automatic-extraction trigger rather than a user-initiated
    one, and `GET /siris/conversations/{id}` confirmed the transcript
    stayed exactly `["user", "assistant"]` -- no extraction step mixed
    in.

62. **Done** -- Diagnostic tools for the "Plex isn't working" flow.
    Follow-up to a large brainstormed list of tool ideas (with the
    project-management-for-GVW-work section explicitly excluded, per
    the request). Rather than build any of it blindly, the list was
    checked against what's already shipped first -- most of the
    "recommended first ten" turned out to already exist (Home
    Assistant, Hermes delegation, notifications, memory/entity
    registry, audit logging, tool-result verification). Asked which
    genuinely-new item to tackle first; the pick was the "Plex isn't
    working" incident recipe the list itself used as its example of
    "SirisAI thinks, tools chain" in action.

    What shipped is deliberately small, matching that same philosophy:
    two new narrow, single-purpose tools, not a bespoke "diagnose an
    incident" skill --
    - `docker_get_container_logs` -- fetches recent stdout+stderr log
      lines from a container, with an optional case-insensitive
      `contains` filter to cut noise before it reaches the model. Adds
      `DockerClient.get_container_logs()`, which has to demultiplex
      Docker's own log-streaming wire format: a non-TTY container's
      logs come back as 8-byte-header-prefixed frames (stream type +
      payload length) rather than plain text, while a TTY container's
      don't -- there's no separate signal for which, so an invalid
      first-byte stream type is treated as "not framed" and the raw
      bytes are decoded as-is.
    - `check_service_endpoint` -- a generic HTTP(S) reachability check
      (reachable/status code/latency only, deliberately never the
      response body, so it can't double as a general "fetch any URL"
      tool) for confirming a dependent service is actually serving
      requests, not just that its container is "running" -- Docker's
      own state can lie about that, the same gap `DockerRestartContainerTool.
      verify()` (milestone 38) already called out for restarts.
      Certificate-expiry checking from the original idea list was
      considered and deliberately left out: httpx doesn't expose the
      peer certificate, so it would need its own raw `ssl` socket
      implementation -- a reasonable follow-up, not bundled in here.

    No new orchestration code ties these together -- the planner
    (already existing, `use_planner=True`) chains `docker_list_containers`
    (existing) -> `docker_get_container_logs` -> `check_service_endpoint`
    -> a delegated fix (`hermes_run_task`/`docker_restart_container`,
    both already existing) entirely through the model's own reasoning,
    exactly the "small dependable tools, no giant monolith" shape asked
    for. `check_service_endpoint` is always registered (it takes its
    target as a call-time argument, unlike every other tool in this
    package which needs a configured `base_url`); the logs tool
    registers alongside the rest of Docker's, gated on
    `SIRISAI_DOCKER_BASE_URL` same as always.

    Verified: full suite (505 passed, up from 496) covering the wire-format
    demuxing for both a framed and a TTY/unframed container, the
    `contains` filter, and `check_service_endpoint`'s reachable/
    unreachable/timeout/non-2xx cases -- including an explicit test that
    a response body can never leak through the tool's return value.
    Live-verified against a real `uvicorn` process, a fake Docker Engine
    API, and a fake Plex endpoint: asked "Plex isn't working" with
    `use_planner: true`, and the planner chained all three tools on its
    own -- `docker_list_containers` (looked "running"), then
    `docker_get_container_logs` with `contains: "error"` (surfaced a
    locked database and a failed transcoder), then `check_service_endpoint`
    (confirmed the endpoint itself was returning 500 despite the
    container's "running" state) -- landing on a correct diagnosis
    entirely from the model's own reasoning. `GET /siris/activity`
    confirmed each step recorded with its own honest
    `"planner: step N of an in-progress plan"` trigger and the prior
    steps' results as `evidence`.

63. **Done** -- Calendar write and conflict checking. The next pick from
    the same brainstormed feature list (milestone 62), chosen as the
    smallest, most contained remaining item: `calendar_upcoming_events`
    (milestone 39) was read-only, with no way to create an event or
    check whether a proposed time was already taken.

    What shipped:
    - `CalendarClient.events_between(start, end)` -- the existing
      `upcoming_events()` query generalized to an arbitrary window
      instead of always "now plus N days" (which now just calls this
      with `start=now`). This turned out to *be* conflict checking:
      "what already exists in this window" is the same query either way.
    - `calendar_check_conflicts` -- a narrow READ tool wrapping
      `events_between()` directly, for "am I free then" questions that
      aren't about creating anything.
    - `calendar_create_event` -- creates an event via `caldav`'s
      `Calendar.add_event()`, on a named calendar or the first available
      one. Deliberately does not refuse or block on a conflict itself --
      its `dry_run()` (reusing the milestone-25 preview mechanism, same
      shape as `DockerRestartContainerTool`'s) reports what would be
      created *and* any conflicting existing events in that window
      up front, so the model (or a human confirming) decides, rather
      than a hardcoded refusal getting in the way of a deliberate
      double-booking. `SAFE_ACTION`, not `CONFIRM_REQUIRED`: an event is
      easy to edit or delete afterwards, and `dry_run` already surfaces
      the one thing worth pausing over.

    Same acknowledged gap as milestone 39 itself (see
    `app/calendar/client.py`'s module docstring): no live CalDAV server
    was available in this environment to verify the real wire protocol
    against, particularly timezone/recurrence edge cases on a specific
    server implementation.

    Verified: full suite (513 passed, up from 505) covering
    `events_between()`'s query window, `create_event()` against the
    default vs. a named calendar and its error for an unknown one, both
    tools' ISO-8601 parsing and delegation, and `dry_run()` reporting
    conflicts (or their absence) without ever calling `create_event()` --
    all mocked at the `caldav.DAVClient` boundary, the same approach
    `test_calendar.py` already used for the read-only tool. Live-verified
    tool registration/schema and the real HTTP round trip through
    `POST /siris/tools/{name}/run` (including `dry_run: true`) against a
    real `uvicorn` process: both tools correctly parsed real ISO-8601
    arguments and surfaced a clean, non-leaking error once they reached
    the (deliberately unreachable, per the gap above) configured CalDAV
    URL.

64. **Done** -- Document search with citations. The next pick from the
    same brainstormed list, scoped down deliberately: asked whether this
    meant building a real multi-source document index/RAG pipeline
    (chunking, embeddings, ranked retrieval -- a multi-week
    architectural project) or enhancing the existing Obsidian
    integration; the answer was the latter -- `obsidian_search`
    already returns a filename and matched context per hit, which *is*
    a citation, it just wasn't shaped like one.

    What shipped, both in `app/obsidian/client.py`:
    - `search()` now normalizes the plugin's raw response (`filename` +
      a `matches` list of `{match: {start, end}, context}` objects) into
      a flat `{path, score, excerpts}` shape -- `path` is exactly what
      to cite ("According to Projects/Router-Manual.md: ..."), `excerpts`
      exactly what to quote, without a caller digging through nesting
      first.
    - `get_note()` now handles PDFs: a manual or contract dropped into
      the vault as a `.pdf` used to come back through a plain UTF-8
      decode as garbage bytes (or a decode error) -- now extracted to
      real text via a new `pypdf` dependency. Any other non-text
      extension deliberately isn't guessed at further: it returns a
      clear `"[Cannot display '{path}': not a text file.]"` placeholder
      rather than mojibake that looks like real content but isn't.

    No new tools, no new settings -- both existing tools
    (`obsidian_search`, `obsidian_get_note`) just got more useful outputs
    from the same inputs, plus updated descriptions telling the model to
    actually cite `path` when answering from a search hit.

    Verified: full suite (516 passed, up from 513) covering the
    normalized search shape (including a multi-excerpt result), PDF text
    extraction (against a hand-built minimal real PDF -- there's no
    PDF-writing library in this project's dependencies, `pypdf` itself
    only reads), and the placeholder path for genuinely undecodable
    binary content. Live-verified against a real `uvicorn` process and a
    fake Obsidian Local REST API server: `obsidian_get_note` correctly
    extracted real text from a real (if minimal) PDF and still returned
    plain markdown unchanged, and `obsidian_search` returned the new
    `path`/`excerpts` citation shape end-to-end through
    `POST /siris/tools/{name}/run`.

65. **Done** -- Result validation for Home Assistant device control.
    First pick from "trustworthiness infrastructure" -- the part of the
    original brainstormed list flagged as "less exciting, but arguably
    the most important." `Tool.verify()` (milestone 38) already existed
    as a mechanism, but only `DockerRestartContainerTool` used it; every
    other consequential tool, including the single most-used
    physical-device tool in the whole system, had none.

    `HomeAssistantCallServiceTool.verify()` closes the exact same gap
    Docker's own restart verification closed, applied to physical
    devices: the service call returning success from Home Assistant's
    API doesn't mean the entity actually changed state -- an
    unreachable or offline device is the common real failure mode, and
    without this a `turn_on` against a dead bulb reports success both
    to the model and in the audit log. Checked only for services with
    one unambiguous expected end state (`turn_on`/`turn_off`,
    `lock`/`unlock`, `open_cover`/`close_cover`, the `arm_*`/`disarm`
    alarm services) and a concrete `entity_id` -- a scene activation, a
    non-boolean `set_temperature`, or a `notify` call has no single
    "correct" resulting state to check against, so those honestly raise
    `NotImplementedError` (this project's established "don't overclaim
    rigor that isn't there" convention) rather than guessing.

    Deliberately scoped to one tool, not "add verify() everywhere" --
    `calendar_create_event` and the Radarr/Sonarr add-movie/add-series
    tools are similarly-shaped future candidates (query back after
    acting to confirm it actually took), left for a later pass rather
    than done as a sweep here.

    Verified: full suite (521 passed, up from 516) covering a
    successful turn_on reaching the expected state, a failed one that
    doesn't (the exact "API said yes, device said no" case), the
    lock/cover domains too, and both `NotImplementedError` cases
    (no `entity_id`; a service with no single expected state).
    Live-verified against a real `uvicorn` process and a fake Home
    Assistant server that accepts a `light.turn_on` call (still returns
    200) while the simulated bulb never actually turns on: `GET
    /siris/activity` showed `verified: false` with `"'light.kitchen' is
    'off', not the expected 'on'"` for that call, and `verified: true`
    for a `lock.lock` call against a device that genuinely reached the
    expected state -- both real HTTP round trips, not mocked at the
    tool boundary.

66. **Done** -- Entity/container name resolution (an ambiguity gate).
    Second pick from "trustworthiness infrastructure." Asked what a
    "confidence and ambiguity gate" should actually mean in this
    architecture first: there's no model-confidence signal to gate on
    here (no logprobs, no self-reported certainty), so a literal
    "block low-confidence actions" gate would be theater. The real,
    buildable gap: `home_assistant_call_service` and
    `docker_restart_container`/`docker_get_container_logs` need an
    *exact* `entity_id`/`container` name, but the model's only path to
    one was dumping a full `home_assistant_list_entities`/
    `docker_list_containers` result and guessing which row matched a
    loose reference like "the office light" -- silently, with no signal
    back when more than one row plausibly matched.

    What shipped: `home_assistant_resolve_entity` and
    `docker_resolve_container`, matching a `name_hint` against
    entity_id/friendly_name or container names/image (case-insensitive,
    every word in the hint must appear somewhere in the haystack) and
    returning *every* match plus an explicit `ambiguous` flag --
    `ambiguous: true` is the gate: their descriptions tell the model to
    ask which one was meant rather than picking one itself when it
    comes back true. Both READ, no new settings, registered alongside
    each integration's existing tools.

    Deliberately not a hard enforcement mechanism -- nothing stops the
    model from skipping straight to `call_service`/`restart_container`
    with a guessed ID anyway, since the underlying tools still accept
    any string. This raises the ambiguity to a first-class, visible
    result instead of leaving it buried in a raw list the model has to
    reason about unprompted; actually blocking the underlying calls
    would need the confirmation layer to know about a resolution step
    that hasn't happened, a bigger, more invasive change not attempted
    here.

    Verified: full suite (529 passed, up from 521) covering matches by
    entity_id vs. friendly_name, matches by container name vs. image,
    the ambiguous-multiple-matches case for both, the domain filter,
    and no-match returning an empty list rather than erroring.
    Live-verified against a real `uvicorn` process and fake Home
    Assistant/Docker servers: "office light" correctly came back
    `ambiguous: true` with both office lights listed, "ceiling" and
    "plex" each correctly resolved to exactly one match -- all through
    real HTTP round trips via `POST /siris/tools/{name}/run`.

67. **Done** -- Bug fix: `recall()` couldn't find memories from a
    natural-language question. Direct bug report: SirisAI had recorded
    the user's name, but asked "what's my name?" it said it couldn't
    recall it. Root cause, in `MemoryStore.recall()`
    (app/memory/service.py): `query` had to appear as one single,
    literal substring of a memory's *entire* content -- fine for a
    caller passing a short keyword, but `ContextEngine.build()` (the
    mechanism that's supposed to make this automatic, no tool call
    needed) passes the user's whole raw prompt as `query` on every
    turn, and "What's my name?" is nowhere close to being a substring
    of "User's name is Brad". The `recall` tool the model can call
    directly had the same fragility if it passed a full question rather
    than a keyword, which its own description didn't warn against.
    SirisAI was then telling the truth about its query finding nothing
    -- just not the truth the user needed, since the fact really was
    there.

    Fix: split `query` into individual significant words and match a
    memory if it contains *any* of them (still plain SQL `ILIKE`, OR'd
    across terms) instead of requiring the whole string as one
    substring -- turns a natural-language question into an actual
    search instead of a near-guaranteed miss, with no change to
    `recall()`'s signature or any caller. A small stopword list (the
    usual pronouns/function words, plus a domain-specific one: "user",
    since nearly every memory here is phrased as a fact *about* "the
    user" and so the word itself carries no signal) and a 3-character
    minimum keep single common words from matching everything; a query
    where every word gets filtered out (e.g. "who am I") falls back to
    the old literal-substring behavior rather than matching
    indiscriminately. Still no embeddings/semantic search and no
    relevance ranking beyond the existing recency order -- both
    explicitly out of scope, called out in the module's own docstring
    as a future real search backend's job, not this fix's.

    One pre-existing test (`test_recall_bumps_usage_on_matching_entries_only`)
    had two memory entries sharing one literal marker string between
    them, relying on the old exact-substring rule to tell them apart --
    updated to use two distinct markers instead, since the point of that
    test (usage bumps only for entries the query actually matches) is
    still true, it just needed data that doesn't accidentally collide
    under the new, intentionally broader matching.

    Verified: full suite (534 passed, up from 529), including new tests
    for matching a multi-word natural-language query, not requiring the
    whole query to match, falling back to literal matching when every
    word is filtered out, and short exact queries still working
    unchanged -- run three times end-to-end against a fresh database
    each time to rule out the shared-word false-positive risk this fix
    trades in for (confirmed stable). Live-verified against a real
    `uvicorn` process and a fake Ollama-compatible backend that echoes
    back its own system message: recorded "User's name is Brad" via
    `POST /siris/memory`, then asked "What is my name?" as a normal chat
    turn -- the context actually sent to the model included
    "Relevant memories: User's name is Brad", and a direct call to the
    `recall` tool with `query: "what is my name"` returned the same
    memory. Neither would have found anything before this fix.

68. **Done** -- Result validation for calendar event creation. The
    other explicitly-flagged follow-up from milestone 65's writeup:
    `calendar_create_event`'s `caldav.add_event()` call not raising
    doesn't prove the server actually persisted anything -- the same
    "success doesn't mean it happened" gap `DockerRestartContainerTool.
    verify()` (38) and `HomeAssistantCallServiceTool.verify()` (65)
    close elsewhere, here for a CalDAV PUT that could silently drop a
    UID collision or reject an unrecognized property without ever
    surfacing an error.

    `CalendarCreateEventTool.verify()` re-queries `events_between()`
    for the same window right after creating and checks the new
    event's `summary` actually comes back. Reuses the same
    `events_between()` milestone 63 already built for conflict
    checking -- no new client method needed.

    Radarr/Sonarr's add-movie/add-series tools remain a similarly-shaped
    candidate, still not done here -- staying with one tool per pass
    rather than a sweep, same choice milestone 65 made explicitly.

    Verified: full suite (536 passed, up from 534) covering both the
    successful (event found) and failed (event never appears) cases,
    mocked at the same `CalendarClient` boundary `test_calendar.py`
    already uses for this tool's other tests. Not live-verified: same
    acknowledged gap as milestones 39/63 -- CalDAV is a real WebDAV
    protocol (PROPFIND/REPORT XML, not simple REST), and there's no
    real CalDAV server in this environment to exercise the actual wire
    behavior against, so a live test here would only prove the fake
    server does what the fake server was told to do.

69. **Done** -- Result validation for Radarr/Sonarr library additions.
    Closes the two remaining candidates explicitly named as "not done
    here" in milestones 65/68's writeups: `radarr_add_movie` and
    `sonarr_add_series`'s own API calls not raising doesn't prove the
    movie/series actually landed in the library -- same "success
    doesn't mean it happened" gap `DockerRestartContainerTool.verify()`,
    `HomeAssistantCallServiceTool.verify()`, and
    `CalendarCreateEventTool.verify()` already close elsewhere. Both
    tools' `verify()` re-query `list_movies()`/`list_series()` (already
    existing methods -- no new client code needed) and check the
    returned id actually appears; a missing `id` in the add response
    (should never happen, but not assumed) is reported unverified
    rather than raising.

    This closes out the whole "does the API success actually reflect
    reality" pass across every tool in this codebase whose own write
    has a natural way to re-check itself: Docker restarts, Home
    Assistant device control, calendar events, and now Radarr/Sonarr
    library adds. Not every write tool gets this -- `hermes_run_task`'s
    result is whatever the delegated instruction produced, with no
    single "did it work" check to run, and `obsidian_create_or_update_note`
    already returns exactly what it wrote with nothing further to
    independently confirm -- verify() stays opt-in for tools with a real
    gap to close, not a blanket requirement.

    Verified: full suite (542 passed, up from 536) covering both
    verify() outcomes for each tool, plus the missing-id case, mocked at
    the same `RadarrClient`/`SonarrClient` transport boundary their
    other tests already use. Live-verified against a real `uvicorn`
    process and fake Radarr/Sonarr v3 API servers -- Radarr's server
    simulated the exact silent-failure case (POST returns success, but
    `GET /api/v3/movie` never lists the new movie): `GET /siris/activity`
    showed `radarr_add_movie` correctly flagged `verified: false`, while
    a genuinely successful `sonarr_add_series` call against a server
    that actually listed the series back came through `verified: true`
    -- both through real HTTP round trips, not mocked at the tool
    boundary.

70. **Done** -- Rollback tool. The last named item from "trustworthiness
    infrastructure": a way to reverse a change, closing the loop the
    verify() sweep (38/65/68/69) opened -- knowing a write didn't
    actually happen is only half the story without a way to undo the
    ones that did.

    `rollback_last_action` (a `Skill`, not a plain `Tool` -- it needs
    `siris.run_tool()` to actually invoke the inverse, the same
    mechanism `PresenceSkill`'s scene activation already uses) looks up
    the most recent recorded call to a named tool via the audit log's
    existing `find_recent()` (28), and if that tool has a registered
    inverse, calls it: `radarr_add_movie`/`sonarr_add_series` -> the
    matching `radarr_delete_movie`/`sonarr_delete_series` using the id
    from the original call's own result; `home_assistant_call_service`
    -> the same domain/entity with the opposite service
    (turn_on<->turn_off, lock<->unlock, open_cover<->close_cover).
    Deliberately a fixed, explicit allowlist, not a generic "figure out
    how to undo anything" mechanism -- guessing an inverse for an
    arbitrary tool (what undoes a Docker restart? a Hermes-delegated
    shell command?) would be worse than admitting it isn't supported, so
    an unlisted tool gets exactly that answer. Also declines cleanly
    when there's nothing to reverse: no recorded call, the original call
    itself errored, or its result doesn't carry enough to act on (e.g.
    no id).

    `rollback_last_action` is itself `CONFIRM_REQUIRED` -- the same tier
    as the delete/control calls it reverses -- but its own single
    confirmation covers the one, already-identified inner action it
    performs, and passes `confirmed=True` down to that specific call
    rather than asking twice for what's really one decision. Same
    reasoning `PresenceSkill`'s auto-confirmed scene call already
    established: the narrow, specific thing being approved *is* the
    approval, not a blank check to do anything -- confirmed here by a
    dedicated test that `run_tool()`-level confirmation is still
    enforced on `rollback_last_action` itself.

    Verified: full suite (550 passed, up from 542; three separate runs
    to rule out flakiness against the shared test audit log) covering
    all three registered inverses actually invoking the right tool with
    the right arguments, the confirmation gate on the skill itself, an
    unsupported tool name, no recorded call, the original call having
    errored, and a result with no id to act on. Live-verified against a
    real `uvicorn` process and fake Radarr/Home Assistant servers:
    added a movie then rolled it back -- the fake Radarr server's own
    log showed a real `DELETE /api/v3/movie/42` -- and turned on a light
    then rolled that back too, with the fake HA server logging a real
    `POST /api/services/light/turn_off`. `GET /siris/activity` showed
    each rollback's inverse call tagged with an honest
    `"rollback_last_action: undoing <tool> (audit id N), itself
    confirmed"` trigger, distinct from the original direct calls.

71. **Done** -- Unified Plex search. Picked up the "Media and lifestyle"
    thread from the original brainstormed list: "Unified media search --
    search Plex, music, podcasts and other libraries conversationally."
    This deployment's only media server is Plex, but Plex itself already
    spans movies, TV, and music in one library -- so "unified" here
    means using Plex's own cross-library search rather than building a
    new aggregation layer across services that don't exist here.

    `PlexClient.search()` calls `/hubs/search`, the same endpoint
    Plex's official clients use for "search everything" -- it already
    groups results into "Hub"s (one per media type) internally, so no
    per-library-section querying is needed. Flattened into one plain
    list with each item's source hub attached as `library`, rather than
    the model having to walk Plex's nested Hub/Metadata structure
    itself. `plex_search` registers alongside the existing Plex tools,
    gated on the same `SIRISAI_PLEX_BASE_URL`/`SIRISAI_PLEX_TOKEN`.

    Verified: full suite (552 passed, up from 550) covering flattening
    multiple hubs into one list with the right `library` attached and a
    hub with no `Metadata` key at all (an empty-results hub Plex's API
    can return) being skipped rather than raising. Live-verified against
    a real `uvicorn` process and a fake Plex server returning results
    across two different hubs (Movies, TV Shows): `plex_search`
    correctly flattened both into one three-item list through a real
    HTTP round trip via `POST /siris/tools/plex_search/run`.

72. **Done** -- Interruption controller for proactive notifications.
    Picked from the "Communications and productivity" thread: "Interruption
    controller — decide whether information is urgent enough to interrupt
    you or should wait for a digest." `POST /siris/proactive/check` used
    to push one combined message for every "notify"-*and*-"urgent" item
    NotificationManager (26) classified, with only the ntfy priority
    distinguishing them (milestone 44) -- routine, non-critical warnings
    still interrupted the user immediately, exactly like a genuinely
    urgent one.

    What shipped: "urgent" items still push right away, unchanged.
    "notify"-tier items no longer push at all -- they're queued
    (`PendingNotification`, `app/notify/pending.py`, a small new table
    alongside `MemoryEntry`/`Event`'s in `app/db/models.py`) and instead
    surface the next time `daily_briefing` runs, folded into its summary
    under "Also since last time: ...", then cleared -- read-once,
    digest-delivered, not left to repeat on every future briefing. A new
    `list_pending_notifications` tool (always registered, independent of
    whether a push channel is configured) lets the model check what's
    queued without waiting for the next briefing either.

    `PendingNotificationStore` is deliberately not user-scoped like
    `MemoryStore` -- one shared queue, not one per user, since these are
    system-level warnings, not personal facts.

    Verified: full suite (560 passed, up from 552) covering the store
    itself (queuing, ordering, clearing, clearing an already-empty
    queue), the tool reading it, `POST /siris/proactive/check` now
    queuing "notify"-tier items instead of pushing them while still
    pushing "urgent" ones immediately, and `daily_briefing` folding
    queued items into its summary and clearing them -- including the
    case where nothing's queued (no stray "Also since last time" line).
    Several pre-existing tests for the old combined-push behavior were
    updated to reflect the new split (e.g. the trigger/evidence test in
    `test_explainable_autonomy.py` now uses an urgent-tier warning, since
    its original notify-tier one no longer triggers a push at all).
    Live-verified against a real `uvicorn` process: seeded a real
    "notify"-tier row directly into the running server's own SQLite file
    (there's no public write endpoint for this queue by design -- it's
    populated only by the proactive check), confirmed
    `list_pending_notifications` saw it, then called the real
    `daily_briefing` tool and confirmed the summary included "Also since
    last time: CPU usage is high: 95%" and the queue was empty
    immediately afterward.

73. **Done** -- House status check. Picked from the same brainstormed
    list, originally framed as "doors, windows, alarms, appliances and
    unusual conditions." "Appliances" and "unusual conditions" are cut
    from this milestone -- there's no appliance-power-draw sensor wired
    up anywhere in this codebase, and "unusual" for a home (as opposed to
    a server, where `ServerHealthSkill` already has concrete thresholds
    to check) isn't something any existing tool measures. Building either
    would mean inventing a signal rather than checking one that exists,
    which this codebase avoids.

    What shipped: a new `house_status_check` skill/tool
    (`app/skills/builtin/house_status.py`), composing existing
    `home_assistant_list_entities` calls only -- no new client, no new
    settings, same shape as `ServerHealthSkill`/`PresenceSkill`. It
    checks three things Home Assistant already exposes honestly: any
    `binary_sensor` whose entity_id matches "door"/"window"/"garage" and
    is `on` (Home Assistant's own convention: a binary_sensor's "on"
    state means the condition is detected, i.e. open, not closed); any
    `lock` entity in the `unlocked` state; and the `alarm_control_panel`
    domain's entities, passed through as-is (their arm state is
    informational, not itself a warning). "Door"/"window"/"garage"
    detection is entity_id substring matching, the same heuristic style
    `docker_resolve_container`/`home_assistant_resolve_entity`
    (milestone 66) already use, since a binary_sensor's device_class
    isn't part of `home_assistant_list_entities`'s trimmed
    entity_id+state shape. Unlike the `home_assistant_*` tools
    themselves, `house_status_check` is always registered -- calling it
    without Home Assistant configured returns
    `{"checked": false, "reason": "Home Assistant is not configured."}`
    rather than the tool simply not existing, so the model can discover
    why rather than getting a missing-tool error.

    Verified: full suite (567 passed, up from 560) in
    `tests/test_house_status.py`, covering the not-configured case, an
    all-secure house (nothing open or unlocked), an open door, an open
    window/garage sensor (with a closed one correctly not flagged), an
    unlocked lock, alarm panels passed through without generating a
    warning of their own, and multiple warnings combining in the
    `warnings` list. Live-verified against a real `uvicorn` process and a
    fake Home Assistant server (`/api/states` returning an open front
    door, an unlocked back door, a locked front door, and an armed alarm
    panel): `POST /siris/tools/house_status_check/run` correctly reported
    `open_openings: ["binary_sensor.front_door"]`,
    `unlocked_locks: ["lock.back_door"]`, and `all_secure: false` through
    a real HTTP round trip; a second run against a server with no Home
    Assistant configured confirmed the tool is still registered and
    returns the not-configured message instead of erroring.

74. **Done** -- Meeting prep. Another narrower productivity pick from the
    same brainstormed list: for calendar events coming up soon, surface
    related Obsidian notes and remembered facts so a meeting doesn't
    start cold.

    What shipped: a new `meeting_prep` skill/tool
    (`app/skills/builtin/meeting_prep.py`), composing existing tools
    only, same shape as `ServerHealthSkill`/`DailyBriefingSkill` -- no
    new client, no new settings. It reuses `calendar_check_conflicts`
    (milestone 63) rather than `calendar_upcoming_events` for the read:
    the latter's `days_ahead` is day-granularity, while meeting prep
    needs an hours-out window, and `calendar_check_conflicts` already
    takes an arbitrary `[start, end)` range -- the same "list events
    overlapping this window" query, just used to prep for what's coming
    rather than to check for a clash. For each event, it searches
    Obsidian (best-effort, same "an unreachable source shouldn't take
    the whole result down" precedent `daily_briefing` established for
    weather/Plex -- skipped outright if not configured, a lookup error
    for one event just leaves that event's notes empty) and recalls
    memories, both keyed on the event's raw `summary` text -- the only
    descriptive field `CalendarClient.events_between` extracts, since
    there's no attendee list to search by instead. An event with no
    summary skips both searches rather than searching on an empty
    string, which would match everything and surface noise instead of
    anything actually related.

    Verified: full suite (576 passed, up from 567) in
    `tests/test_meeting_prep.py`, covering the not-configured case, no
    upcoming events, an event with nothing related found, related
    memories and related notes each surfacing correctly, notes being
    omitted when Obsidian isn't configured, surviving Obsidian being
    unreachable, and an event with no summary skipping both searches
    entirely (asserting neither `obsidian_search` nor `memory.recall`
    was even called, not just that the result came back empty).
    Live-verified against a real `uvicorn` process: with Obsidian pointed
    at a real fake server, `POST /siris/tools/meeting_prep/run` correctly
    registered `meeting_prep` alongside the calendar and Obsidian tools;
    with no calendar configured at all it returned the honest
    not-configured message through a real HTTP round trip. Same
    acknowledged gap as milestones 39/63/68 -- no live CalDAV server was
    available in this environment -- so with a calendar URL configured
    (deliberately unreachable, per that gap) the endpoint correctly
    returned a clean 502 rather than a leaking stack trace, the same
    verification milestone 63 used for `calendar_create_event` itself.

75. **Done** -- Conversation handoff between rooms. Scoped down from the
    ambiguous "conversation handoff" brainstorm item via a direct
    question: it means following you physically between satellites
    (kitchen to bedroom, mid-conversation), not summarizing a
    conversation for another person or exporting session state between
    clients -- this codebase already has the room/satellite plumbing
    (milestone 48) to make the first one real.

    The gap: each satellite process keeps its own `conversation_id` only
    in its own memory (`satellite/siris_satellite/client.py`). Walk from
    the kitchen's satellite to the bedroom's and the bedroom process has
    never seen a conversation_id -- its next `POST /siris/chat` omits
    one, which silently started a brand-new conversation, losing
    whatever was just discussed, even though the server already tracks
    which room was most recently active (`SatelliteStore.current_room()`,
    milestone 48).

    What shipped: `SatelliteRegistration` gets a new
    `last_conversation_id` column (migration patch in
    `app/db/migrate.py`, same pattern as every prior added column) --
    `record_activity()` now optionally records which conversation was
    last in play from a room, alongside the timestamp it already bumped.
    A new `SatelliteStore.handoff_candidate(exclude_room, within_seconds)`
    finds the most recently active *other* room's conversation, but only
    if that activity is recent enough (`SIRISAI_SATELLITE_HANDOFF_WINDOW_SECONDS`,
    default 120s) to plausibly be the same person having just walked
    over, not an old conversation resurfacing hours later. `POST
    /siris/chat` calls it exactly when the ambiguity it's meant to
    resolve actually exists: `room` is set and no usable
    conversation_id came with the request. A client with no `room` (the
    web UI, or any non-satellite caller) is completely unaffected --
    the new logic only ever runs in the one case it exists to fix.
    Deliberately no staleness cutoff on `current_room()` itself (per its
    own docstring, an old answer for "where do I push a reply" is still
    right) -- a handoff is a stronger claim ("you're still mid-thought"),
    so it gets its own bound rather than inheriting that one.

    Verified: full suite (584 passed, up from 576, stable across 3
    repeated runs) in `tests/test_satellites.py`, covering
    `record_activity()` recording (and, when omitted, preserving)
    `last_conversation_id`, `handoff_candidate()` picking the right
    other room, ignoring one with no recorded conversation, returning
    `None` with no other rooms at all, and (against an isolated store)
    ignoring activity older than the window. One caught-live test
    isolation issue: an end-to-end "stale conversation isn't handed
    off" test against the real `/siris/chat` route was flaky --
    this file's `client` fixture is session-scoped, sharing one
    satellite registry across every test in it, so an unrelated,
    still-fresh room left behind by an *earlier* test could legitimately
    outrank the deliberately-backdated one and mask the exact thing
    being tested. Dropped in favor of the isolated-store unit test,
    which proves the same cutoff deterministically. Live-verified
    against a real `uvicorn` process with a fake Ollama backend: registered
    a kitchen and a bedroom satellite, sent `POST /siris/chat` to the
    kitchen with no conversation_id (got a fresh one back), then to the
    bedroom with no conversation_id of its own -- got the *same*
    conversation_id back, and `GET /siris/conversations/{id}` confirmed
    both rooms' turns landed in one real, persisted conversation. A
    third call with no `room` at all correctly started an unrelated,
    separate conversation, confirming non-satellite clients are
    unaffected.

76. **Done** -- Extend the `Tool.verify()` sweep to Home Assistant
    automations. With the brainstormed feature list now exhausted down
    to items needing either hardware confirmation (room-presence,
    speaker identification) or a new, unconfigured integration (email
    triage) neither of which had been given, the next self-directed pick
    was closing a gap this codebase's own hardening pattern had already
    flagged as unfinished: `HomeAssistantCallServiceTool.verify()`
    (milestone 65) covers one-off service calls, but
    `HomeAssistantCreateAutomationTool`/`HomeAssistantDeleteAutomationTool`
    (both `CONFIRM_REQUIRED`, milestone 54/a later hardening pass) had
    none -- create/delete_automation() hit Home Assistant's undocumented
    config-editor endpoint (see `app/home_assistant/client.py`'s module
    docstring: not part of the officially documented REST API), so a 200
    there is exactly the same "the call not raising doesn't prove it
    actually happened" gap `DockerRestartContainerTool.verify()`
    (milestone 38) exists for, just unaddressed for the two automation
    tools until now.

    What shipped: both tools' `verify()` re-list `domain='automation'`
    entities and check the automation_id against each one's `id`
    attribute -- present after create, absent after delete. Same
    NotImplementedError convention as every other `verify()` in this
    codebase when there's nothing to check (no `automation_id` in the
    result/arguments).

    Verified: full suite (590 passed, up from 584) covering both
    success and failure cases for create (`automation.id` found —> found)
    and delete (`automation.id` gone —> gone), plus the
    NotImplementedError case for a result/arguments missing
    automation_id, all mocked at the `HomeAssistantClient` boundary,
    same approach `test_home_assistant.py` already used for
    `HomeAssistantCallServiceTool.verify()`. Live-verified against a
    real `uvicorn` process and a fake Home Assistant server that
    actually persists automation config server-side (not just echoing
    200): `POST /siris/tools/home_assistant_create_automation/run`
    followed by `GET /siris/activity` showed `verified: true` with
    detail "Automation \<id\> confirmed present"; deleting it the same
    way showed `verified: true` with "Automation \<id\> confirmed
    removed" -- both through the same automatic `SirisAI.run_tool()` ->
    `Tool.verify()` -> audit-log pipeline every other verified tool call
    in this codebase already goes through.

77. **Done** -- Extend the `Tool.verify()` sweep to
    `obsidian_create_or_update_note`. Continuing the same self-directed
    hardening pass as milestone 76: this tool is `CONFIRM_REQUIRED`
    precisely because a full-replace write is genuinely destructive with
    no built-in undo (its own module docstring), yet it had no `verify()`
    -- a 204 from Obsidian's Local REST API plugin doesn't prove the
    note's real content actually changed (a sync conflict or a
    read-only vault mount could silently no-op the write while the
    plugin still reports success), the same "the API call not raising
    doesn't prove it happened" gap this sweep already closed for Docker
    restarts (38), Home Assistant service calls (65), and Home Assistant
    automations (76).

    What shipped: `verify()` re-fetches the note via `get_note()` and
    compares it byte-for-byte against the `content` that was written.
    Deliberately raises `NotImplementedError` for a `.pdf` path --
    `get_note()` extracts PDFs to plain text via `pypdf` rather than
    returning raw bytes (milestone 64), so comparing that against the
    markdown content this tool wrote would never match even on a fully
    successful write. That's not something to report as a false
    failure; it's the same "nothing honest to check" case every other
    `verify()` in this codebase already raises for.

    Verified: full suite (593 passed, up from 590) covering the match,
    mismatch, and `.pdf`-path-raises-`NotImplementedError` cases, mocked
    at the `ObsidianClient` boundary, same approach `test_obsidian.py`
    already used for the tool's own write test. Live-verified against a
    real `uvicorn` process and a fake Obsidian server that actually
    stores PUT content and serves it back on GET (not just echoing
    204): `POST /siris/tools/obsidian_create_or_update_note/run`
    followed by `GET /siris/activity` showed `verified: true` with
    detail "'Projects/SirisAI.md' now matches the written content" --
    through the same automatic `run_tool()` -> `verify()` -> audit-log
    pipeline as every other verified tool call.

78. **Done** -- Extend the `Tool.verify()` sweep to Radarr/Sonarr's
    delete and remove-queue-item tools. Milestone 69 gave
    `radarr_add_movie`/`sonarr_add_series` a `verify()` but left their
    own mirror-image operations --
    `radarr_delete_movie`/`sonarr_delete_series` and
    `radarr_remove_queue_item`/`sonarr_remove_queue_item`, all four
    `CONFIRM_REQUIRED` -- unverified. A 200 from a DELETE call doesn't
    prove the movie/series is actually out of the library, or the queue
    item actually gone (it could even be silently re-added by Radarr/
    Sonarr's own re-grab behavior when not blocklisted) -- the same "the
    call not raising doesn't mean it happened" gap this sweep has
    already closed for adds, calendar events, Home Assistant service
    calls/automations, and Obsidian note writes.

    What shipped: each `verify()` re-lists the relevant collection
    (`list_movies()`/`list_series()` for delete,`queue()` for
    remove-queue-item) and confirms the id is now absent -- the inverse
    check of what `radarr_add_movie`'s `verify()` already does for
    presence. Same `NotImplementedError` convention when there's no id
    to check against.

    Verified: full suite (605 passed, up from 593) covering
    success/failure for all four tools plus the
    `NotImplementedError`-without-an-id case, mocked at the
    `RadarrClient`/`SonarrClient` boundary. Live-verified against a real
    `uvicorn` process and fake Radarr/Sonarr servers that actually
    track library/queue state server-side (not just echoing 200):
    `POST /siris/tools/radarr_delete_movie/run` and
    `radarr_remove_queue_item/run`, then the same two calls against
    Sonarr, each followed by `GET /siris/activity` showing `verified:
    true` with the matching "confirmed removed" detail -- all four
    through the same automatic `run_tool()` -> `verify()` -> audit-log
    pipeline.

79. **Done** -- Extend the `Tool.verify()` sweep to
    `tautulli_terminate_session`. The last remaining `CONFIRM_REQUIRED`
    tool in this codebase without one: Tautulli's `terminate_session`
    call is relayed on to Plex, and the actual player client is what has
    to honor it -- a network hiccup or an unresponsive client can leave
    the stream still playing even though Tautulli itself accepted the
    command, the same "the API call not raising doesn't prove it
    happened" gap this sweep has now closed everywhere it applies in
    this codebase.

    What shipped: `verify()` re-checks `current_activity()` for the same
    `session_id` -- gone means the stream actually stopped, still
    present means it didn't. Same `NotImplementedError` convention when
    there's no `session_id` to check against.

    Verified: full suite (608 passed, up from 605) covering the
    success/failure/no-`session_id` cases, mocked at the
    `TautulliClient` boundary. Live-verified against a real `uvicorn`
    process and a fake Tautulli server that actually tracks active
    sessions server-side and removes one on `terminate_session` (not
    just echoing success): `POST /siris/tools/tautulli_terminate_session/run`
    followed by `GET /siris/activity` showed `verified: true` with
    detail "Session abc123 confirmed no longer active" -- through the
    same automatic `run_tool()` -> `verify()` -> audit-log pipeline as
    every other verified tool call.

80. **Done** -- UniFi client block/unblock. Direct request to keep
    developing the UniFi integration specifically. UniFi has been
    read-only since milestone 57, which explicitly declined to add a
    write action "without the means to re-verify it against real
    hardware from here" -- that constraint still holds in this
    environment, so before building anything the user was asked
    directly: expand read-only coverage instead, or add write actions
    anyway despite being unable to verify the exact endpoint shape
    against a real controller. The answer was to add them anyway.

    What shipped, in `app/unifi/client.py`: `block_client()`/
    `unblock_client()`, POSTing `{"action": "BLOCK"}`/`{"action":
    "UNBLOCK"}` to the Integration API's per-client action endpoint --
    modeled on the action-based shape developer.ui.com/network/
    integration documents for device actions (e.g. restart), applied
    here to clients, but genuinely not live-tested against a real
    controller the way this integration's read paths were (milestone
    56). `list_clients()` now also exposes `id` (needed to target a
    block/unblock call at all -- it wasn't returned before) and
    `blocked`. Two new `CONFIRM_REQUIRED` tools,
    `unifi_block_client`/`unifi_unblock_client`, in
    `app/plugins/builtin/unifi.py`.

    Deliberately scoped to clients only, not devices: restarting a
    UniFi device (AP, switch, gateway) risks taking down the LAN
    segment this very SirisAI instance may be running on, a materially
    larger blast radius than disconnecting one client device -- left
    for a future milestone to weigh on its own rather than folded in
    here.

    `verify()` re-lists clients and checks the `blocked` flag -- but
    honestly asymmetric about what "client not found in the list"
    means, since that's genuinely ambiguous without a real controller
    to confirm against: after blocking, the client vanishing from the
    "connected clients" list is *consistent* with a successful block
    (disconnected), but could just as easily mean it was already
    offline before the call -- there's no way to tell which from here,
    so that case honestly raises `NotImplementedError` rather than
    guessing either way. After unblocking, the same absence is
    unambiguous (nothing has reconnected yet, which doesn't contradict
    success), so that direction reports `verified: True`. Also added
    `unifi_block_client`/`unifi_unblock_client` as a new inverse pair to
    `RollbackLastActionSkill`'s `_INVERSE_HANDLERS` (milestone 70) --
    the same "opposite action" pattern already used for Home Assistant
    services, applied here since blocking has as obvious a real inverse
    as `turn_on`/`turn_off`.

    Verified: full suite (621 passed, up from 608) covering
    `list_clients()`'s new fields, block/unblock posting the right
    action body, both tools' `verify()` success/failure/no-id cases,
    the block-side `NotImplementedError`-on-absence case specifically,
    and a new rollback test proving `unifi_block_client` reverses
    through `unifi_unblock_client` end-to-end via
    `RollbackLastActionSkill`. Live-verified against a real `uvicorn`
    process and a fake UniFi controller that actually tracks a client's
    blocked state server-side (not just echoing 200): `POST
    /siris/tools/unifi_block_client/run` followed by `GET
    /siris/activity` showed `verified: true` with "Client client-1
    confirmed blocked"; unblocking showed the mirror result; and
    calling `rollback_last_action` for `unifi_block_client` actually
    called `unifi_unblock_client` against the real fake controller,
    confirmed by re-querying its client list directly and seeing
    `"blocked": false`.

81. **Done** -- UniFi client ambiguity resolution. Direct follow-up to
    keep developing the UniFi integration: `unifi_block_client`/
    `unifi_unblock_client` (80) need an exact `client_id`, which nothing
    before this tool could produce from a loose description like "the
    kid's phone" without the model guessing at raw `unifi_list_clients`
    output itself -- the same gap milestone 66 closed for Home Assistant
    entities and Docker containers with
    `home_assistant_resolve_entity`/`docker_resolve_container`.

    What shipped: `unifi_resolve_client`, `READ`, matching loose words
    against both a client's name and IP address (the two fields
    `unifi_list_clients` already exposes that a person would plausibly
    describe a device by -- "the kid's phone", or its IP if that's what
    they know it by). Returns every match plus an `ambiguous` flag,
    same shape as the two existing resolve tools, so the model asks
    which one was meant rather than guessing when there's more than
    one.

    Verified: full suite (625 passed, up from 621) covering matching by
    name, matching by IP address, flagging ambiguous for multiple
    matches, and no matches at all. Live-verified against a real
    `uvicorn` process and the same fake UniFi controller from milestone
    80: `POST /siris/tools/unifi_resolve_client/run` with `name_hint:
    "kid phone"` correctly resolved to the one real client through a
    real HTTP round trip, and a nonsense hint correctly came back with
    an empty match list rather than erroring.

82. **Done** -- UniFi client reconnect. Continuing the UniFi thread:
    asked directly whether the next step should be a UniFi *device*
    restart (explicitly flagged, again, as materially riskier -- could
    take down the LAN segment SirisAI itself runs on, and with lower
    confidence in the exact endpoint shape than client actions ever
    had) or something else; the answer was a lower-risk alternative --
    forcing one *client* to reconnect, for troubleshooting a flaky
    device rather than restricting access.

    What shipped: `UniFiClient.reconnect_client()` posts
    `{"action": "RECONNECT"}` to the same per-client action endpoint
    block/unblock already use, and a new `unifi_reconnect_client` tool.
    Still `CONFIRM_REQUIRED` despite the smaller blast radius than
    block -- it still has a real, immediate effect on whoever's
    currently using the device, the same tier reasoning as block/
    unblock -- but deliberately has no `verify()`: unlike blocking
    (which leaves a persistent `blocked: true` to check), a forced
    reconnect has no lasting flag to confirm against, the same "no
    single correct end state within this call" gap
    `HomeAssistantCallServiceTool.verify()` already raises
    `NotImplementedError` for (a scene activation, a non-boolean
    `set_temperature`) -- adding a check here that couldn't mean
    anything would be worse than honestly having none.

    Verified: full suite (653 passed, up from 650) covering the request
    shape, the `CONFIRM_REQUIRED` tier, and that `verify()` correctly
    raises `NotImplementedError` rather than fabricating a result.
    Live-verified against a real `uvicorn` process and the same fake
    UniFi controller from milestones 80-81: `POST
    /siris/tools/unifi_reconnect_client/run` succeeded through a real
    HTTP round trip, `GET /siris/activity` showed `verified: null` (no
    check attempted, not a silently-skipped one), and re-querying the
    fake controller directly confirmed the client's `blocked` state was
    untouched by the reconnect.

83. **Done** -- UniFi device restart. The one remaining UniFi gap,
    flagged as materially riskier twice (milestones 82 and, before that,
    milestone 57's original read-only decision) since it could take
    down the LAN segment SirisAI itself runs on. Asked directly a third
    time whether to build it, whether to move to a different part of
    SirisAI instead, or stop for now -- the answer was to go ahead.

    What shipped: `UniFiClient.restart_device()` posts
    `{"action": "RESTART"}` to a device-scoped action endpoint (the
    same action-endpoint shape as the client actions), and a new
    `unifi_restart_device` tool, `CONFIRM_REQUIRED` like every other
    write in this file. `list_devices()` now also exposes `id` (needed
    to target a restart at all -- it wasn't returned before).

    Two things make this tool different from the client-scoped ones:

    - A `dry_run()` (the same preview mechanism
      `DockerRestartContainerTool` established, milestone 25) reports
      the target device's current name/model/state, and -- best-effort,
      not authoritative -- flags when the model name matches known
      UniFi gateway/router prefixes (`UDM`/`UDR`/`UXG`/`USG`). A
      gateway is the single point of internet/LAN routing for the
      whole site, a categorically bigger deal than restarting one AP
      or switch, and the human confirming should see that before
      approving, not discover it after. An unrecognized model name
      just means no warning fires, rather than a false claim either
      way.
    - Deliberately **no** `verify()`: a real device reboot is a full
      hardware/firmware boot cycle, on the order of a minute or more --
      nothing like a Docker container's few-second process restart.
      Checking the device's state immediately after this call returns
      would see it still rebooting essentially every time, reporting
      "not verified" on every successful restart. That's worse than
      not checking at all, so this honestly has nothing to check
      within the call rather than a check that would systematically
      lie.

    Verified: full suite (660 passed, up from 653) covering the
    request shape, the `CONFIRM_REQUIRED` tier, `verify()` correctly
    raising `NotImplementedError`, `dry_run()` reporting current state
    for a found and an unknown device, and the gateway warning firing
    for a `UDM-Pro` model but not a `USW-24-PoE` switch. Live-verified
    against a real `uvicorn` process and a fake UniFi controller
    tracking two real devices (an AP and a `UDM-Pro` gateway):
    `dry_run: true` correctly returned no warning for the AP and the
    full gateway warning (naming the model) for the gateway, both
    through real HTTP round trips; actually restarting the AP returned
    `verified: null` in `GET /siris/activity` (no check attempted, not
    a silently-skipped one) and re-querying the fake controller
    directly showed its state had genuinely transitioned to
    `REBOOTING` -- concretely demonstrating why an immediate `verify()`
    would have been wrong.

84. **Done** -- rollback support for `home_assistant_create_automation`.
    With UniFi's own tool surface now complete (milestones 57/80-83), the
    next gap examined was whether Cleanuparr or Listenarr had a similar
    well-scoped expansion available -- both turned out not to: unlike
    UniFi, which had real published API docs to build against (just no
    live hardware to test), their own docstrings say there's no reference
    documentation at all to build further tools from, so expanding either
    now would mean fabricating endpoints rather than extending a
    known-but-unverified surface, which those same docstrings already
    name as the wrong way to close that gap. Looked instead at
    `RollbackLastActionSkill`'s `_INVERSE_HANDLERS` allowlist (milestone
    70) for a tool with an obvious, safe inverse that wasn't registered
    yet -- `home_assistant_create_automation`/`home_assistant_delete_automation`
    fit exactly.

    What shipped: a new `_inverse_home_assistant_create_automation`
    handler, the same "the result carries the id needed to build a
    delete call" shape `_inverse_radarr_add_movie`/
    `_inverse_sonarr_add_series` already use --
    `create_automation()`'s result has `automation_id` merged in, which
    becomes `home_assistant_delete_automation`'s argument. Deliberately
    one-directional, like those two: `home_assistant_delete_automation`'s
    own recorded arguments are just an `automation_id`, with no alias/
    trigger/action/condition/mode to recreate the automation from, so
    there's nothing safe to build a reverse-of-the-reverse from -- the
    same "admit it isn't supported rather than guess" principle this
    skill's own module docstring already states.

    Verified: full suite (661 passed, up from 660) covering the new
    handler end-to-end -- creating an automation against a fake Home
    Assistant instance, then rolling it back and confirming the delete
    call actually reached it. Live-verified against a real `uvicorn`
    process and a fake Home Assistant server: `POST
    /siris/tools/home_assistant_create_automation/run` created a real
    automation (confirmed via the fake server's own `/api/states`), `GET
    /siris/activity` showed `verified: true` for the create; `POST
    /siris/tools/rollback_last_action/run` with `tool_name:
    "home_assistant_create_automation"` then issued a real `DELETE` to
    the fake server, which subsequently reported an empty automation
    list, and the delete's own `verify()` also reported `verified: true`.

85. **Done** -- `dry_run()` for `obsidian_create_or_update_note`. With
    the rollback allowlist now covering every safely-invertible
    CONFIRM_REQUIRED tool (milestone 84), looked at `dry_run()` coverage
    instead: `Tool.dry_run()`'s own docstring calls it deliberately
    optional ("most tools have nothing meaningful to simulate"), so this
    isn't a sweep to fill in everywhere -- only
    `obsidian_create_or_update_note` had a preview genuinely worth
    adding. Unlike a restart, which always affects *some* existing
    thing, this tool's own description covers two different cases: a
    plain create (nothing to lose) and a full-content replacement
    (Obsidian has no built-in undo, per this file's module docstring) --
    and there was no way to tell which one a given call would be before
    it happened.

    What shipped: `dry_run()` fetches the target path and reports
    whether it already exists; when it does, the response includes the
    note's current content (what a replacement would actually discard)
    and an `unchanged` flag. A 404 is treated as "doesn't exist yet" --
    `would_create: true`, nothing to preview -- any other error still
    raises, the same "don't paper over a real failure as a clean
    not-found" distinction `UniFiClient` makes elsewhere. Same
    "surface the risk before confirming, not after" reasoning as
    `UniFiRestartDeviceTool.dry_run()`'s gateway warning.

    Verified: full suite (665 passed, up from 661) covering both
    branches (new path, existing path with matching/differing content)
    and that a non-404 error still propagates. Live-verified against a
    real `uvicorn` process and a fake Obsidian Local REST API server
    seeded with one existing note: `dry_run: true` against the existing
    path correctly reported `would_overwrite: true` with its real
    current content, and against a new path correctly reported
    `would_create: true` with no content to show; actually writing the
    existing path afterward produced a real `PUT` the fake server
    processed (confirmed via a direct `GET` against it), with
    `verify()` reporting `verified: true` in `GET /siris/activity`.

86. **Done** -- `event_reaction_check`: proactive reaction to incoming
    webhook events. `POST /siris/events`' own module docstring left
    "deciding whether to ignore, investigate, or proactively notify
    based on an incoming event" as a later milestone deliberately: an
    incoming event can come from *any* external system with a
    completely freeform description, unlike every other proactive skill
    here (server_health_check, event_timing_check, goal_check_in,
    new_device_alert), each of which checks one specific, known domain
    against a fixed, deterministic rule. Asked directly how to close
    that gap -- a deterministic keyword scan, consistent with
    `NotificationManager`'s own "simple keyword/pattern matching, not
    fuzzy judgment" design, versus an LLM judgment call on each incoming
    event -- the answer was the deterministic keyword scan.

    What shipped:
    - `EventReactionSkill` (`event_reaction_check`), composed into
      `POST /siris/proactive/check` alongside the other proactive
      skills: scans events logged since the last check for a small,
      fixed "something broke" keyword list ("error", "failed",
      "failure", "critical", "offline", "unreachable", "down"),
      case-insensitive against the event's own description.
    - A monotonic cursor (`EventMemoryStore.get_reaction_cursor`/
      `advance_reaction_cursor`, backed by a new single-row
      `EventReactionCursor` table) tracks the highest event id already
      classified -- the same "persists across restarts" reasoning
      `KnownNetworkDevice` uses for `new_device_alert`, so a scheduled
      `/check` run doesn't re-flag the same old event forever.
      `EventMemoryStore.events_since()` is the new query backing it,
      oldest-first and bounded by a `limit` the same way
      `distinct_recurring_signatures()`'s `scan_limit` bounds an
      unprocessed backlog.
    - `NotificationManager` gained a new `"event"` category for
      `"Event flagged: ..."` messages -- deliberately never escalates
      to critical/urgent, the same conservative stance `new_device_alert`'s
      `"network"` category already takes, only stronger: a keyword hit
      against a completely freeform description carries less confidence
      than `server_health_check`'s own fixed, known message shapes.

    Deliberately a small, fixed keyword list, not a generic classifier:
    an event phrased without any of these words goes unflagged, a real,
    accepted false-negative rate rather than an ever-growing list that
    still couldn't cover every external system's own vocabulary.

    Verified: full suite (680 passed, up from 665) covering the skill's
    keyword matching (case-insensitivity, with/without a source,
    cursor-bounded "only new events"), the cursor/`events_since()`
    methods against an isolated in-memory DB (the same isolation
    `PendingNotificationStore`'s own tests use, since this cursor is a
    single global row), the new `NotificationManager` category, and the
    `/check` endpoint composing it in. Live-verified against a real
    `uvicorn` process: logged a benign event and a "NAS went offline"
    event via `POST /siris/events`, then `POST /siris/proactive/check`
    correctly flagged only the second one, classified it `category:
    "event"`/`action: "notify"` (queued, not pushed as urgent); a second
    `/check` call re-flagged nothing; logging a third event afterward and
    checking again picked up only that new one -- confirming the cursor
    genuinely advances rather than re-scanning from the start each time.

87. **Done** -- `dry_run()` for `radarr_delete_movie`/`sonarr_delete_series`.
    With `event_reaction_check` closing the last "later milestone"
    breadcrumb left in the codebase, swept the same "does this
    CONFIRM_REQUIRED tool have a `dry_run()` genuinely worth adding"
    question (milestone 85's reasoning, not milestone 65/76's `verify()`
    sweep) across every remaining tool -- most already have one where it
    matters, or (`Tool.dry_run()`'s own docstring) genuinely don't need
    one. `radarr_delete_movie`/`sonarr_delete_series` were the exceptions:
    both delete-with-optional-`delete_files` a library entry, the same
    no-undo shape `obsidian_create_or_update_note`'s full-replacement
    case already got a preview for, and their own `list_movies()`/
    `list_series()` already carry exactly the fields (title/year,
    `has_file` for movies) a preview needs.

    What shipped: both `dry_run()`s report the target's title/year and
    whether `delete_files` would actually do something --
    `radarr_delete_movie`'s can say so precisely (`has_file` tells it
    whether there's a file to delete at all); `sonarr_delete_series`'s
    honestly can't (`list_series()` carries no per-series file-presence
    flag) so it only reports the flag as given, not a confirmed
    prediction -- the same "don't overclaim rigor that isn't there"
    restraint `UniFiRestartDeviceTool.dry_run()`'s best-effort gateway
    warning already uses.

    Verified: full suite (687 passed, up from 680) covering both tools'
    `dry_run()` for a found movie/series (with and without `delete_files`)
    and an unknown id. Live-verified against a real `uvicorn` process and
    a fake Radarr/Sonarr server seeded with one movie (`has_file: true`)
    and one series: `dry_run: true` correctly reported
    `would_delete_downloaded_file: true` for the movie and
    `would_delete_downloaded_files: true` for the series, both through
    real HTTP round trips; an unknown movie id correctly reported
    `has_file: null`/`would_delete_downloaded_file: false`; actually
    deleting the movie afterward produced a real `DELETE` the fake
    server processed (confirmed via a direct `GET` showing an empty
    library) with `verify()` reporting `verified: true`.

88. **Done** -- `position`: a stable, gapless memory number distinct
    from `id`. Asked to renumber memories from 1, staying gapless as
    entries are added/deleted. The literal ask -- physically renumbering
    the `memories` table's real primary key -- was flagged as risky
    before building it: `id` is the only handle
    `DELETE /siris/memory/{id}` accepts (the documented "find
    duplicates via self_maintenance_check, then delete by id" workflow),
    so renumbering it on every add/delete means an id fetched from one
    call could point at a different memory (or nothing) by the time it's
    used to delete, and could race with a concurrent request doing the
    same. Asked which was actually wanted -- the real id renumbered, or
    just a clean display number -- and the answer was the display
    number.

    What shipped: every memory dict (`remember`/`recall`/`list_all`, and
    the write-time-dedup path in `remember()` that can return an
    existing entry instead of a new one) now also carries `position` --
    a 1-based, gapless rank among that user's memories, oldest first.
    `MemoryStore._position_map()` computes it fresh on every call (one
    extra query, ranking by `created_at` then `id` as a tiebreak) rather
    than storing or persisting it anywhere -- always correct after any
    add/delete without ever touching another row's actual `id`, which
    stays a normal, permanent, stable primary key exactly like every
    other id in this codebase (movies, series, automations, ...).

    Verified: full suite (692 passed, up from 687) covering positions
    assigned oldest-first starting at 1, `recall()` attaching the
    correct position independent of its own most-recent-first result
    order, a delete closing the gap for the remaining entries, a
    subsequent add continuing the sequence (not reusing the freed
    number) while getting a fresh, never-reused `id`, and the API
    response/`list_all()` both exposing the new field. Live-verified
    against a real `uvicorn` process: created three memories via
    `POST /siris/memory` (positions 1/2/3), deleted the middle one via
    `DELETE /siris/memory/{id}` (`204`), confirmed `GET /siris/memory`
    now showed the remaining two renumbered to 1/2 with their original
    ids (1 and 3) untouched, then created a fourth memory and confirmed
    it correctly got position 3 (continuing the sequence) with a brand
    new id (4) rather than reusing the freed one.

89. **Done** -- Frigate integration: SirisAI's first vision tools.
    Asked to brainstorm ideas grounded in the wider "Jarvis" concept and
    then build several of them; this is the one flagged as the single
    biggest gap relative to that concept -- every existing integration
    reasons over text/state from other services, never over what a
    camera actually saw.

    What shipped: `app/frigate/client.py` + `app/plugins/builtin/frigate.py`,
    registered when `SIRISAI_FRIGATE_BASE_URL` is set -- two READ tools,
    `frigate_list_cameras` (from Frigate's own config) and
    `frigate_recent_events` (recent detections, optionally filtered by
    camera/label, each with a `snapshot_url`/`clip_url` built only when
    Frigate actually saved one). Built from Frigate's own published REST
    API docs (https://docs.frigate.video/integrations/api/) -- a real,
    documented spec, closer to UniFi's tier than Cleanuparr/Listenarr's
    guessed one, just with no live camera hardware here to confirm the
    exact response shape against. Deliberately narrow on this first
    pass: camera list + recent events, not Frigate's full surface
    (recordings, live streams, review items) -- the same restraint this
    codebase's other least-verified integrations already use, rather
    than guessing at endpoints not yet confirmed necessary.

    One deliberate deviation from every other integration here:
    `SIRISAI_FRIGATE_API_KEY` is optional, and registration is gated on
    `base_url` alone. A typical self-hosted Frigate instance has no
    built-in API authentication at all (its own docs recommend a
    reverse-proxy for that) -- requiring a key here would make the
    common case impossible to configure. When a key is given it's sent
    as a bearer token, for setups that do front it with one.

    Verified: full suite (704 passed, up from 692) covering both tools,
    camera enabled-state defaulting, event field mapping (including
    Frigate's Unix-timestamp times converted to this codebase's usual
    ISO 8601), snapshot/clip URLs present only when Frigate reports
    having saved one, camera/label/limit filtering, and the optional-
    auth behavior (no header sent when no key configured, a bearer
    token when one is). Live-verified against a real `uvicorn` process
    and a fake Frigate server seeded with two cameras and two events:
    `frigate_list_cameras` and `frigate_recent_events` both returned
    correct data through real HTTP round trips, including a
    person-detection event with real snapshot/clip URLs and a
    dog-detection event correctly showing `null` for both (Frigate
    hadn't saved media for it) and camera-filtering narrowing results
    correctly.

90. **Done** -- `trust_ledger`: a queryable rollup of `verify()`
    success rates. Second item from the same Jarvis-concept brainstorm --
    this codebase already records `verified`/`verification_detail` on
    every audit log entry for tools that define `Tool.verify()` (the
    verify() sweep, milestones 65/76), but nothing before this rolled
    that up into "which tools' real-world verification is actually
    succeeding" -- each tool's trustworthiness was scattered across
    individual `GET /siris/activity` rows, not a single place to look.

    What shipped: `AuditLogStore.verification_stats_by_tool()`
    aggregates `verified` outcomes per tool among the most recent 500
    executions -- attempts, verified-true/false counts, and a success
    rate -- sorted worst-success-rate first, same
    extend-what's-already-recorded reasoning `failure_counts_by_tool()`
    (milestone 30) already uses for failures. Surfaced via a new
    `trust_ledger` tool (`app/plugins/builtin/audit.py`, always
    registered alongside `explain_recent_action`), READ, no parameters.
    Only tools that have actually attempted `verify()` at least once
    appear -- a tool with no `verify()` at all (most of them) has
    nothing meaningful to report.

    Verified: full suite (709 passed, up from 704) covering: only
    verify()-attempting tools appear, correct success-rate computation,
    worst-first sorting, and the tool's registration/read-permission and
    that it actually reads through to the audit store (unique tool
    names per test, same shared-audit-log-noise-avoidance pattern
    `test_self_maintenance.py`'s `failure_counts_by_tool` tests already
    use). Live-verified against a real `uvicorn` process and a fake
    Radarr server rigged to demonstrate the exact gap `verify()` exists
    to catch: deleting one movie that genuinely disappears (`verified:
    true`) and a second whose `DELETE` returns `200` without actually
    removing it (`verified: false`, the "API call not raising doesn't
    prove it happened" case `radarr_delete_movie.verify()`'s own
    docstring describes) -- `trust_ledger` correctly reported
    `radarr_delete_movie` at 2 attempts, 1 true, 1 false, a 0.5 success
    rate.

91. **Done** -- Geofence-triggered reminders. Third item from the same
    Jarvis-concept brainstorm: "remind me when I get home" / "let me
    know whenever Brad leaves work," built entirely on data Home
    Assistant already exposes (a `person`/`device_tracker` entity's
    `state` is its current zone) -- no new external dependency, unlike
    the commute/traffic-nudge idea from the same brainstorm, which is
    on hold pending a real traffic API + key.

    What shipped:
    - A new `GeofenceReminder` model + `GeofenceReminderStore`
      (app/geofence/): `entity_id` + `zone` + `message` + `repeat`,
      scoped per user like `GoalStore`. `create_geofence_reminder`,
      `list_geofence_reminders`, `set_geofence_reminder_enabled` --
      always registered, same "store exists regardless of whether HA
      is configured yet" split `GoalStore`/the planner already draws.
    - `GeofenceReminderCheckSkill` (`geofence_reminder_check`),
      registered only alongside Home Assistant (needs
      `home_assistant_get_state`): generalizes `PresenceSkill`'s own
      hardcoded household UniFi home/away trigger (milestone 42) to an
      arbitrary entity/zone/message, same transition-only discipline
      (only fires on the tick a zone actually changes into the watched
      one, not every check while already there) and same "first check
      establishes a baseline, doesn't assume a transition" reasoning.
      Delivers via `notify` directly rather than through
      `NotificationManager` -- a reminder's message is text the user
      already wrote and approved, not an auto-generated warning needing
      severity classification.
    - By default a reminder is one-shot: it disables itself
      (`enabled=False`) the moment it fires, like a real reminder
      shouldn't refire forever; `repeat=true` makes it a standing rule
      instead.
    - A new `POST /siris/proactive/geofence` route, 404 when Home
      Assistant isn't configured -- its own endpoint rather than folded
      into `/check`, same reasoning `/presence` already uses: this
      isn't a system-health warning for `NotificationManager` to score.
    - One entity failing to look up (a stale/deleted `entity_id`) is
      skipped rather than aborting the whole check -- one broken
      reminder shouldn't block every other one's.

    Verified: full suite (729 passed, up from 709) covering the store
    (create/list/enable-disable/record_zone), tool registration and
    permission tiers, and the check skill's full behavior matrix
    (baseline establishment, no-change, genuine transition firing +
    notifying, non-repeat self-disabling vs. repeat staying enabled,
    transitioning to an unwatched zone, already-in-zone not refiring,
    firing even without `notify` configured, one broken entity not
    blocking others, disabled reminders skipped) plus the endpoint's
    404/200 behavior. Live-verified against a real `uvicorn` process, a
    fake Home Assistant server (with a `/control/set_state` endpoint to
    simulate a real zone transition), and a fake ntfy server: created a
    reminder for `person.brad` entering `home`, a first
    `POST /siris/proactive/geofence` established the baseline (`work`,
    no fire) through real HTTP round trips, moving the fake entity to
    `home` and checking again correctly fired -- a real push landed on
    the fake ntfy server with the reminder's exact message -- the
    reminder then showed `enabled: false` in `list_geofence_reminders`,
    and a third check stayed quiet.

92. **Done** -- Time-of-day metric anomaly detection. Fourth item from
    the same Jarvis-concept brainstorm: `server_health_check`'s existing
    personalized threshold (milestone 27) already learns a per-server
    "normal" from recent history, but as one flat mean/stdev across a
    server's full day/night cycle -- a value that's unremarkable
    against that full-cycle average can still be genuinely unusual for
    *this specific hour* (e.g. a server that idles near 0% overnight
    spiking to 40% at 3am), and the flat baseline's own day/night swings
    push its threshold too high to ever catch that.

    What shipped: `MetricsStore.hourly_baseline(metric_name, hour, ...)`
    -- the same mean/stdev `baseline()` already computes, narrowed to
    samples recorded during one specific UTC hour. `ServerHealthSkill.
    _check_metric()` now computes both and prefers the hourly one
    whenever it has enough samples (a lower `min_samples` than the flat
    baseline's, since an hourly bucket only ever gets ~1/24th as many
    samples over the same wall-clock time as the flat baseline does),
    falling back to the flat baseline, then the fixed 90% ceiling, in
    the same order the flat baseline already fell back to the fixed
    ceiling. The warning message keeps its exact
    `"{label} is high: {value}%"` prefix regardless of which threshold
    triggered it -- `NotificationManager`'s correlation regex still
    matches -- with a `(hourly baseline ...)` or `(overall baseline
    ...)` suffix saying which one actually fired.

    Verified: full suite (733 passed, up from 729) covering
    `hourly_baseline()` directly (below-min-samples, correctly ignoring
    other hours' samples, mean/stdev computation) and
    `ServerHealthSkill` preferring and correctly labeling the hourly
    baseline when available. Live-verified against a real `uvicorn`
    process: seeded a tight, low (~1%) disk-usage baseline for the
    current hour directly in the database before the very first real
    health check, then called `server_health_check` -- the real disk
    usage (20.5%, unremarkable on its own) was correctly flagged as
    anomalous for this specific hour, with the warning explicitly
    labeled `hourly baseline 4.3% ± 8.0%, 6 samples`.

93. **Done** -- Deeper cross-domain correlation: events join the world
    model. Fifth item from the same Jarvis-concept brainstorm --
    scoped down from the original idea (correlating "internet's slow"
    with "3 devices downloading," which needs per-device bandwidth data
    this codebase doesn't collect anywhere) to something real:
    `event_reaction_check`'s flagged events had a named entity
    (`Event.entity_name`) sitting right there, unused by
    `NotificationManager`'s existing container+disk correlation.

    What shipped:
    - `event_reaction_check`'s warning messages now end with
      `(entity: X)` when the event was logged against a named entity --
      the same kind of anchor a container name or disk path already is.
    - `NotificationManager._correlate()` generalized from
      "container is the only cause, disk is the only partner" to two
      small helper functions (`_cause_entity_name`/`_partner_entity_name`)
      -- a flagged event's entity is now eligible on *both* sides: as
      the cause whose world-model relationships get looked up (same
      role a container name plays), and as a partner other items get
      matched against (same role a disk path plays). A container name
      is still only ever a cause, never a partner, exactly as before
      this change -- narrowly extending what correlates with what, not
      turning this into a generic graph match over every pairing.

    Verified: full suite (739 passed, up from 733) covering the new
    message suffix (present/absent) and four correlation shapes: event
    as cause with a related disk partner, event as partner of a docker
    cause, no correlation without a recorded relationship, and a direct
    check that a container name is still never extracted as a partner
    entity. Live-verified against a real `uvicorn` process: recorded a
    `nas DEPENDS_ON nas-storage` relationship via
    `remember_entity_relationship`, logged two independently-flagged
    events (`entity_name="nas"` and `entity_name="nas-storage"`) via
    `POST /siris/events`, then `POST /siris/proactive/check` correctly
    merged both into one `category: "correlated"` notification citing
    the relationship, instead of surfacing as two seemingly unrelated
    warnings.

94. **Done** -- First-class scenes: a named, ordered bundle of Home
    Assistant service calls the model can save once and trigger as one
    unit. Sixth item from the same Jarvis-concept brainstorm ("scenes,
    not raw HA automation YAML") -- `home_assistant_create_automation`
    already covers "run this when X happens"; a scene is the other
    half, "run this now, by name," without re-describing every step
    each time.

    What shipped:
    - `Scene` (`app/db/models.py`) -- id, name, and a JSON-encoded
      ordered list of steps, each shaped exactly like
      `home_assistant_call_service`'s own `domain`/`service`/
      `entity_id`/`service_data` arguments. Not user-scoped, same
      reasoning as `Routine`: a scene is shared home infrastructure.
    - `SceneStore` (`app/scenes/service.py`) -- `create_scene`,
      `list_scenes`, `get_scene`, `delete_scene`. Deliberately just a
      store; it doesn't call Home Assistant or decide when to run
      anything.
    - `create_scene`/`list_scenes`/`delete_scene` tools
      (`app/plugins/builtin/scenes.py`), registered only alongside Home
      Assistant itself (a scene with nowhere to run isn't worth being
      able to create).
    - `run_scene` (`app/skills/builtin/run_scene.py`) -- a `Skill`
      rather than a raw `Tool`, since it composes multiple
      `siris.run_tool()` calls (one `home_assistant_call_service` per
      step): a `Tool` subclass takes specific, typed collaborators, not
      a generic `siris` reference, so "chain several tool calls into
      one" is exactly what the `Skill` abstraction is for. Along the
      way, `Skill`/`SkillTool` gained optional `dry_run()`/`verify()`
      forwarding (mirroring `Tool.dry_run()`/`Tool.verify()`'s own
      "raises `NotImplementedError` unless overridden" convention) so a
      skill can support them too -- fully backward-compatible, every
      existing `Skill` subclass that doesn't override them behaves
      identically to before. `run_scene` is `CONFIRM_REQUIRED` (it can
      call anything `home_assistant_call_service` could, across every
      step); its `dry_run()` previews the scene's name and every step
      before confirming; its `verify()` honestly raises
      `NotImplementedError` -- a scene's steps can span any mix of
      domains/services, each with its own idea of "did this work" (or
      none at all), the same "genuinely ambiguous outcome" gap
      `HomeAssistantCallServiceTool.verify()` already leaves open on
      its own less-than-fully-deterministic calls.

    Verified: full suite (754 passed, up from 739) covering the store,
    the CRUD tools' registration/permissions/round-trip, and
    `run_scene`'s `run()`/`dry_run()`/`verify()` behavior. Live-verified
    against a real `uvicorn` process with a fake Home Assistant server:
    created a two-step `movie_night` scene (`light.turn_off` then
    `media_player.turn_on`), dry-ran it and saw both steps previewed
    without any HTTP call reaching the fake server, ran it unconfirmed
    and got `confirmation_required` back, then ran it confirmed and
    confirmed the fake server received both service calls in order
    (`GET /__calls__` on the fake server showed exactly the two POSTs,
    in step order), and finally deleted the scene and confirmed
    `list_scenes` no longer returned it.

95. **Done** -- Goal decomposition check: flag active goals that are
    still just a title. Seventh item from the same Jarvis-concept
    brainstorm (chief-of-staff category) -- `create_goal`/`add_objective`
    already let the model decompose a goal into concrete steps whenever
    it likes, so this doesn't add any new decomposition capability;
    it's the deterministic missing half, noticing a goal that never got
    that treatment, the same "flag it, don't fix it" boundary
    `GoalCheckInSkill`'s staleness check already draws.

    What shipped:
    - `GoalStore.goals_needing_decomposition()`
      (`app/goals/service.py`) -- active goals with zero objectives.
      Distinct from `stale_goals()`: a goal created five minutes ago
      with no objectives shows up here immediately, long before it
      would ever go stale.
    - `GoalDecompositionCheckSkill`
      (`app/skills/builtin/goal_decomposition.py`, name
      `goal_decomposition_check`) -- same thin-wrapper shape as
      `GoalCheckInSkill`, always registered, composed into
      `POST /siris/proactive/check` alongside it. Its warning messages
      start with `Goal '...'`, so `NotificationManager` classifies them
      under the existing `"goals"` category/`"notify"` action for free
      -- no new classification rule needed.

    Verified: full suite (763 passed, up from 754) covering the store
    method (flags a bare goal, excludes one with an objective, excludes
    a completed goal, un-flags once an objective is added), the skill,
    its `NotificationManager` classification, and the `/check` endpoint
    composition. Live-verified against a real `uvicorn` process: created
    one bare goal and one with an objective via `create_goal`, confirmed
    `POST /siris/proactive/check` flagged only the bare one (queued as a
    `"goals"`/`"notify"` item, not pushed urgent), then called
    `add_goal_objective` on it and confirmed a follow-up check dropped
    it from `goals_needing_decomposition`.

96. **Done** -- `list_capability_gaps`: chat-time parity with
    `GET /siris/capability_gaps`. `report_capability_gap` (milestone 31)
    has always persisted "I have no tool for that" reports, and a human
    could always read them back via the REST endpoint -- but the model
    itself had no way to query its own backlog mid-conversation (e.g.
    "what have you already flagged as missing?"). Same "give the model
    the same read access a human already has via REST" shape every other
    `list_*` tool already follows.

    What shipped:
    - `ListCapabilityGapsTool` (`app/plugins/builtin/capability_gap.py`,
      READ, always registered) -- thin wrapper over the pre-existing
      `CapabilityGapStore.list_gaps()`, same store `GET
      /siris/capability_gaps` already calls.
    - `list_capability_gaps` added to `_CATEGORY_NAMES`
      (`app/core/siris.py`) alongside `report_capability_gap`, so both
      group under the same `"capabilities"` category in
      `GET /siris/capabilities`.

    Verified: full suite (768 passed, up from 763) covering registration,
    a report-then-list round trip through the tool, `limit` handling, and
    an explicit parity check that the tool and the REST endpoint return
    the same set of ids for the same data. Live-verified against a real
    `uvicorn` process: reported a gap via `report_capability_gap`,
    confirmed `list_capability_gaps` and `GET /siris/capability_gaps`
    returned identical results, and confirmed `limit` and the
    `"capabilities"` grouping both behaved correctly.

97. **Done** -- A "Gaps" tab in the built-in web UI
    (`app/web/static/index.html`), user-requested: a visible register of
    what SirisAI has reported it can't do yet, updating the same way the
    Memory tab does -- read straight from `GET /siris/capability_gaps`,
    the same endpoint milestone 96 gave the model chat-time access to.
    No backend changes needed -- both the auto-population
    (`report_capability_gap`, milestone 31) and the read endpoint
    already existed; this was purely wiring the existing UI's tab
    pattern (nav button + `section.view` + a `load*()`/`render*()` pair
    hitting `api()`) to data that had no UI view yet.

    What shipped:
    - A `Gaps` nav button and `#gaps-view` section, between Memory and
      Activity.
    - `loadGaps()`/inline rendering (mirrors `loadActivity()`'s
      read-only + Refresh-button shape, not Memory's add-form -- entries
      here are auto-reported by the model via `report_capability_gap`,
      never hand-added by a user): each `.gap-entry` shows the requested
      capability, the suggested integration when one was given, and an
      `#id · timestamp` meta line, most recent first.

    Verified: full suite still green (768 passed -- this is a pure
    frontend change, no new backend surface to test). Live-verified with
    a real `uvicorn` process and a real headless Chromium (Playwright):
    reported two gaps (one with a suggested integration, one without)
    via `report_capability_gap`, opened the Gaps tab, and confirmed both
    entries rendered with the right text (screenshot-checked); confirmed
    Chat/Memory/Activity/Status all still worked after the nav change
    (no regression from adding a fifth tab).

98. **Done** -- `resolve_capability_gap`/`rebuild_and_restart`: closing
    the loop from a reported gap to a deployed fix, via Hermes Agent.
    User-requested, and scoped down deliberately during the ask: rather
    than a fully autonomous report-to-deploy pipeline, the human chose
    "Hermes drafts and merges without a review prompt, but the
    rebuild/restart still needs an explicit go-ahead every time" -- so
    that's the exact shape this shipped as. Also adds
    `dismiss_capability_gap`, the plain "not doing this" close that
    doesn't involve Hermes at all -- the other half of "a way to clear a
    capability gap" this milestone was scoped from.

    Why SirisAI can't just do this to itself: it's the thing running
    *inside* the container a fix might need to rebuild, so both the code
    change and the redeploy are necessarily delegated to Hermes Agent
    (app/hermes/client.py), a separate agent on the same host with its
    own terminal/git access -- the same structural reason Docker/system
    control was already delegated to Hermes rather than reimplemented.

    What shipped:
    - `CapabilityGap` gained `status` ('open' default, then 'resolved'
      / 'failed' / 'dismissed') and `resolution_detail` columns
      (`app/db/models.py`, patched into existing DBs via
      `app/db/migrate.py`).
    - `CapabilityGapStore` (`app/capabilities/service.py`): `get_gap()`,
      `dismiss()`, `mark_resolved()`, `mark_failed()`, and an optional
      `status` filter on `list_gaps()`.
    - `dismiss_capability_gap` (SAFE_ACTION, always registered) --
      closes a gap with no Hermes involvement.
    - `resolve_capability_gap` (SAFE_ACTION, registered only alongside
      Hermes) -- looks up the gap, and if it's still 'open' (a
      non-open gap is left alone and reported back as-is, never
      re-attempted), sends Hermes a built instruction: implement on a
      new branch, add tests, run the full suite, merge to main only if
      everything passes, and end the reply with an exact
      `RESULT: MERGED` / `RESULT: NOT_MERGED: <reason>` marker line.
      SirisAI has no independent way to check whether a merge actually
      happened -- that access lives on Hermes's side -- so the marker
      is parsed with a plain substring check, and a reply with neither
      marker is treated as *not* merged, never assumed successful.
    - `rebuild_and_restart` (`app/plugins/builtin/hermes.py`,
      CONFIRM_REQUIRED, registered only alongside Hermes) -- delegates
      pulling `main` and rebuilding/restarting the container to Hermes.
      The one place in this whole feature with a mandatory human
      confirmation, by design: whatever is currently on `main` is what
      goes live, and the assistant is briefly unavailable during the
      rebuild.
    - Gaps tab: a status badge and `resolution_detail` per entry,
      "Attempt fix"/"Dismiss" buttons on open gaps (shown only when the
      corresponding tool is actually registered, checked via
      `GET /siris/status`), and a "Rebuild & restart SirisAI" button
      (shown only when `rebuild_and_restart` is registered) that
      confirms via a native browser dialog before calling the tool with
      `confirmed: true`.
    - A follow-up while scoping this out: without a push, nothing would
      ever tell a human a fix is ready -- they'd have to remember to
      check the Gaps tab themselves. A successful merge now fires a
      notification (when a notify channel is configured); when
      `SIRISAI_PUBLIC_BASE_URL` is also set, it carries a one-tap
      "Rebuild now" ntfy action button posting straight to
      `POST /siris/tools/rebuild_and_restart/run` with
      `confirmed: true` -- the exact same `http` action-button
      mechanism `RoutineRunner._notify_pending_confirmation()`
      (app/routines/runner.py) already uses for a routine's own pending
      confirmations, reused rather than inventing a second way to build
      one. A failed/not-merged resolution never notifies -- there's
      nothing to deploy.

    Verified: full suite (793 passed, up from 768) covering the store's
    lifecycle methods, the status filter, both new tools' registration
    gating (dismiss always on, resolve/rebuild only with Hermes
    configured), permission tiers, a fake-Hermes-backed resolve that
    parses MERGED/NOT_MERGED/no-marker-at-all into the right status each
    time, the already-resolved short-circuit never calling Hermes again,
    rebuild_and_restart's CONFIRM_REQUIRED gate and instruction content,
    and the notification behavior (plain notify with no
    `SIRISAI_PUBLIC_BASE_URL`, the action-button shape with one set,
    never sent on a failed/not-merged resolution, and never raising
    when no notify channel is configured at all). Live-verified against
    a real `uvicorn` process wired to a fake Hermes server (an
    `httpx`-shaped OpenAI chat-completions endpoint returning canned
    MERGED/NOT_MERGED replies by instruction content) and a fake ntfy
    server: reported four gaps, resolved one to "resolved" and one to
    "failed" via the API, dismissed a third, confirmed a fourth's
    "Attempt fix" button in a real headless Chromium drove it from
    "open" to "resolved", confirmed `rebuild_and_restart` refused to run
    unconfirmed and then actually delegated to Hermes once confirmed,
    confirmed re-resolving an already-resolved gap never issued a second
    Hermes call, and -- with `SIRISAI_PUBLIC_BASE_URL` set -- confirmed
    the merge notification carried a working "Rebuild now" action
    button: posting exactly the URL/body it specified actually triggered
    the rebuild, while the failed resolution sent no notification at
    all.

99. **Done** -- Memory tab: display `position`, not the raw database
    `id`. User-reported bug: the Memory tab's numbering looked "out of
    order." The actual cause -- `MemoryEntryResponse.position` (a
    stable, gapless, 1-based rank recomputed fresh on every
    `GET /siris/memory` call, an earlier milestone) had been sitting
    unused in the response the whole time; `renderMemory()`
    (`app/web/static/index.html`) was still displaying `entry.id`, the
    real primary key, which is never reused and has a gap wherever a
    memory was ever deleted -- exactly what "out of order" looks like
    to someone watching the list. One-line fix: display
    `entry.position` instead. No backend change needed; the field was
    already there.

    Verified: full suite still green (793 passed -- a pure frontend
    change, no new backend surface). Live-verified against a real
    `uvicorn` process and a real headless Chromium: created four
    memories (positions 1-4), deleted the second one via
    `DELETE /siris/memory/{id}`, and confirmed the Memory tab showed a
    gapless #1-#3 (no #4) for the three survivors -- then added a fifth
    memory through the UI's own Add form and confirmed it automatically
    became #4, with every other entry's number unchanged.

100. **Done** -- `delete_memory`: user-requested, a deliberate reversal
    of `MemoryStore.delete()`'s original "not exposed as a model tool
    yet" decision from when deletion was first added purely for manual
    duplicate cleanup. Also adds a Delete button to the Memory tab,
    which had a REST delete endpoint underneath it (used by that
    original cleanup workflow) but no UI to reach it from.

    What shipped:
    - `DeleteMemoryTool` (`app/plugins/builtin/memory.py`, SAFE_ACTION,
      always registered -- same permission tier as `delete_scene`: a
      memory is low-stakes to lose, even though unlike a scene there's
      no undo) -- wraps the pre-existing `MemoryStore.delete()`, the
      same method `DELETE /siris/memory/{id}` already used. Its
      description is explicit that it takes a memory's `id`, not its
      `position` -- position is a display-only rank that shifts as
      memories are added or deleted, so passing it as if it were the id
      would delete the wrong memory or fail unpredictably.
    - `delete_memory` added to `_CATEGORY_NAMES` under `"memory"`,
      alongside `remember`/`recall`.
    - Memory tab: a "Delete" button per entry, confirmed via a native
      browser dialog before calling `DELETE /siris/memory/{id}` --
      always the entry's real `id`, never its displayed `position`, and
      the list reloads (renumbering gaplessly) afterward.
    - Updated `MemoryStore.delete()`'s and the REST route's own
      docstrings, which still described the pre-reversal "not exposed
      as a model tool, deliberately" reasoning -- now stale.

    Verified: full suite (799 passed, up from 793) covering tool
    registration, permission tier, the capabilities category, a
    round trip through the tool (create, delete via
    `POST /siris/tools/delete_memory/run`, confirm it's gone), an
    unknown-id failure, and a full chat-turn test with a fake LLM client
    proposing `delete_memory` end to end. Live-verified against a real
    `uvicorn` process and a real headless Chromium: created three
    memories, clicked "Delete" on the middle one, confirmed the native
    browser dialog fired, confirmed it was actually gone, and confirmed
    the two survivors renumbered gaplessly to #1/#2 with no #3.

101. **Done** -- Fuzzy Home Assistant entity matching, plus honest
    scoping on a second, user-reported issue that turned out not to be
    fixable in code. User report: (1) "turn off the lounge lamps" works
    but "turn the lounge lamps off" doesn't, and (2) entity matching
    should tolerate "something similar" to an entity's real name, not
    require it verbatim.

    Investigated (1) first, since a real bug would need fixing before
    anything else: grepped the entire request pipeline
    (`app/cortex/orchestrator.py`, `app/context/engine.py`,
    `app/planning/planner.py`) for any deterministic parsing of the
    user's raw text -- keyword lists, regex, an intent classifier --
    and found none. Every "turn on"/"turn off" in this codebase is
    Home Assistant's own service-name string (API plumbing), never a
    match against what the user actually typed; the model itself
    chooses `service='turn_on'` vs `'turn_off'` from the whole sentence
    via ordinary tool-calling, the same as any other tool argument.
    That means phrase-order sensitivity isn't a SirisAI bug to fix --
    it's the configured LLM's own language understanding, and no code
    change here can correct it. Two honest, limited things still
    shipped for it: `home_assistant_call_service`'s own description now
    explicitly states both phrasings mean the same thing (a cheap
    nudge that may help a smaller/weaker model, not a guaranteed fix),
    and this writeup exists so the real lever -- the LLM backend, not
    this codebase -- is on record rather than silently unaddressed.

    (2) was genuinely fixable: `HomeAssistantResolveEntityTool` required
    every word of a name hint to be a literal substring of the entity's
    id/friendly name -- "lounge lamps" (plural) would never match an
    entity literally named "Lounge Lamp", and a one-letter typo would
    never match at all. `_entity_name_matches()`
    (`app/plugins/builtin/home_assistant.py`) now also accepts a close
    spelling match (`difflib.get_close_matches`, cutoff 0.8) against any
    word actually in the entity's id/friendly name when the literal
    substring check fails -- catching typos and simple plurals (ratio
    ~0.89-0.91) while still rejecting a genuinely different word like
    "lamp" vs "light" (ratio ~0.22). Deliberately spelling-tolerance,
    not synonym understanding: a real "lamp means light to me" mapping
    would need semantic/embedding search, a much larger change than
    what "a slight variation" asked for. The existing `ambiguous: true`
    safety net is unchanged and still catches any case fuzzy matching
    makes genuinely unclear (verified live below) -- widening the match
    turns a would-be miss into "ask the user," never a wrong guess.

    Verified: full suite (803 passed, up from 799) covering the
    existing exact-match tests unchanged, plus new cases for a plural
    variant, a one-letter typo, a genuinely different word correctly
    NOT matching, and multiple fuzzy matches still flagging `ambiguous`.
    Live-verified against a real `uvicorn` process and a fake Home
    Assistant server with two real-shaped entities ("Lounge Lamp" and
    "Lounge Light"): `"lounge lamps"` and `"loung lamp"` both resolved
    cleanly to the lamp alone; `"lounge light"` correctly came back
    `ambiguous: true` over both entities rather than silently guessing
    (the domain-name-as-keyword behavior that makes "light" match any
    light-domain entity is pre-existing, not something this change
    introduced -- the ambiguity flag was already the correct safety net
    for it).

102. **Done** -- `home_assistant_call_service` fuzzy fallback: recover
    from a guessed `entity_id` that doesn't exist. User-requested
    follow-up to milestone 101's fuzzy entity matching, which only
    helped when the model called `home_assistant_resolve_entity` first
    -- if it skipped that and passed a slightly-wrong `entity_id`
    straight to `home_assistant_call_service` (e.g. `light.loung_lamp`
    for a real `light.lounge_lamp`), the call just silently failed or
    no-op'd against Home Assistant with no recovery.

    What shipped:
    - `HomeAssistantClient.get_state_or_none()` (`app/home_assistant/client.py`)
      -- like `get_state()`, but returns `None` instead of raising on a
      404, so the tool doesn't need its own try/except at the call site.
    - `HomeAssistantCallServiceTool.run()` now checks whether a given
      `entity_id` actually exists before calling the service; if not,
      `_resolve_fallback()` reuses milestone 101's own
      `_entity_name_matches()` against every same-domain entity (the
      guessed id's own words as the needle, exactly like
      `home_assistant_resolve_entity`'s own matching) and substitutes it
      *only* when exactly one candidate matches. Zero or multiple
      candidates leave `entity_id` untouched -- same "don't guess among
      several" caution as `resolve_entity`'s own `ambiguous` flag, so
      the worst case is identical to before this change (a no-op/fail),
      never a wrong device.
    - A substitution wraps the return value (`{"result", "resolved_entity_id",
      "note"}`) so both the audit log and `verify()` can see what
      actually ran -- `verify()` now checks the resolved entity_id when
      one is present, not the original (nonexistent) one, which would
      otherwise always "fail" even though the real target changed state
      exactly as expected. The common case (entity already existed) is
      completely unaffected -- same bare return shape as before.

    Verified: full suite (809 passed, up from 803) covering the
    existence check itself, a single-candidate substitution (and that
    it's logged and returned correctly), the zero- and multiple-candidate
    cases leaving `entity_id` untouched, a domain-wide call with no
    `entity_id` skipping the check entirely, and `verify()` correctly
    checking the resolved entity. Live-verified against a real `uvicorn`
    process and a fake, stateful Home Assistant server (its `light.turn_off`
    handler actually flips the entity's tracked state, so `verify()` has
    something real to check): calling with `entity_id='light.loung_lamp'`
    against a real `light.lounge_lamp` entity correctly substituted,
    ran, and verified true against the resolved entity; calling with a
    genuinely unrelated `entity_id` left it untouched and behaved
    identically to before this change (including `verify()`'s own
    pre-existing 404-on-a-nonexistent-entity error, unchanged).

103. **Done** -- Web UI parity for Goals, Routines, and Scenes: three new
    tabs so they're usable without chat or `curl`. All three were already
    fully built on the backend (milestones 23/29 and the Scenes work) --
    only reachable indirectly until now.

    Backend: one new route, `GET /siris/scenes`
    (`app/api/routes/scenes.py`) -- the direct-read equivalent of
    `list_scenes`, same relationship `GET /siris/routines` already has to
    `list_routines`. Registered unconditionally (`SceneStore` itself has
    nothing to do with Home Assistant, see `app/scenes/service.py`) even
    though the create/delete/run *tools* only register alongside it.
    `GET /siris/goals` and `GET /siris/routines` already existed, so
    nothing else was needed on the read side. Every mutating action
    (`create_goal`, `add_goal_objective`, `complete_goal_objective`,
    `update_goal_status`, `create_routine`, `set_routine_enabled`,
    `create_scene`, `delete_scene`, `run_scene`) is invoked from the UI via
    the existing generic `POST /siris/tools/{name}/run` endpoint rather
    than a dedicated route each -- the same pattern the Gaps tab already
    established for `resolve_capability_gap`/`dismiss_capability_gap`/
    `rebuild_and_restart`, so no new mutating routes were needed either.

    Frontend (`app/web/static/index.html`):
    - **Goals tab**: create by title, a status filter (active/all/
      completed/abandoned), a checkbox per objective (`complete_goal_objective`),
      an inline "add objective" row, and Mark completed/Abandon/Reactivate
      buttons driving `update_goal_status`.
    - **Routines tab**: create form (name, trigger description, interval
      minutes, comma-separated tool names), an Enable/Disable toggle per
      routine, and a "Run due now" button hitting the existing
      `POST /siris/routines/run_due`.
    - **Scenes tab**: registered only with Home Assistant configured, so
      the tab checks `GET /siris/status`'s tool list first and shows a
      plain notice instead of a dead create form when unavailable --
      same feature-detection the Gaps tab already does for
      `rebuild_and_restart`. Create takes a name plus a steps *JSON*
      array (parsed client-side; each step is exactly
      `home_assistant_call_service`'s own shape, so no step-builder UI
      was invented for it), and each scene gets Run (a native `confirm()`
      dialog, then `run_scene` called with `confirmed: true` -- the
      dialog *is* the human approval `CONFIRM_REQUIRED` needs, the same
      pattern already used for `rebuild_and_restart`) and Delete buttons.

    Verified: full suite (825 passed, up from 823) including two new
    route tests confirming `GET /siris/scenes` works without Home
    Assistant configured and reflects a directly created scene.
    Live-verified against a real `uvicorn` process (Home Assistant
    pointed at a fake, stateful server) driving the exact REST calls the
    UI's JS makes for all three tabs end to end: created a goal with an
    objective, added a second objective, completed the first, marked the
    goal completed, and confirmed the active-only filter then excluded
    it; created a routine, confirmed a disabled routine is skipped by
    `run_due`, re-enabled it, and confirmed `run_due` then actually ran
    it; created a scene, confirmed an unconfirmed `run_scene` call
    correctly comes back `confirmation_required` (never runs
    unconfirmed), then confirmed `confirmed: true` runs it and the fake
    Home Assistant server's tracked entity state actually flips, and
    confirmed delete removes it from the list.

104. **Done** -- Cron overview: a read/toggle window onto crontab entries
    on other machines on the network (a home Linux server, Raspberry
    Pis), in a new Web UI tab. User-requested, and explicitly distinct
    from Routines: "I don't want routines" -- these are cron jobs that
    already exist independently on other machines, not something SirisAI
    schedules or runs itself, so this is pure read (plus enable/disable),
    never create.

    Backend:
    - `SIRISAI_CRON_HOSTS` (`app/core/config.py`) -- comma-separated
      `user@host` (or `user@host:port`) entries, e.g.
      `brad@192.168.0.127,root@192.168.0.83`. Deliberately no separate
      SSH key-path setting: `CronHostsClient` (`app/cron/client.py`)
      shells out to the system `ssh` binary rather than pulling in a
      Python SSH library, so it picks up whatever identity file/agent/
      known_hosts already grants SirisAI's own host user access to reach
      these machines -- the same access already used to reach them any
      other way (the user's own reasoning for this: "Hermes should be
      able to access both of these").
    - Only each host's own connecting-user crontab (`crontab -l`/
      `crontab -`) is read/written -- not `/etc/crontab` or
      `/etc/cron.d/*`, which would need root on hosts this doesn't have
      it on and a riskier whole-file rewrite. Connecting as root (one of
      the two hosts this was built for) already covers root's own
      crontab, which is where most Pi automation typically lives anyway.
    - `cron_list_jobs` (READ) parses every configured host's crontab into
      `{host, schedule, command, line, enabled}` entries -- `line` is the
      stable identifier `cron_set_job_enabled` (SAFE_ACTION) uses to find
      and toggle (comment out/uncomment) the right line, rather than a
      position that could shift between listing and toggling. A host
      that fails to connect contributes one `{host, error}` entry instead
      of failing the whole list -- one unreachable Pi doesn't blank the
      overview. Both tools, and `GET /siris/cron_jobs` (the direct-read
      equivalent of `cron_list_jobs`, same relationship `GET
      /siris/scenes` has to `list_scenes`), only do anything once
      `SIRISAI_CRON_HOSTS` is set; the route returns `[]` rather than
      erroring when it isn't, same "empty = feature not engaged"
      convention used elsewhere.
    - Web UI: a new Cron tab (feature-detected via `/siris/status`'s tool
      list, same pattern the Gaps tab already uses for
      `rebuild_and_restart`), grouped by host, each entry showing
      schedule/command and an Enable/Disable button calling
      `cron_set_job_enabled` through the existing generic
      `POST /siris/tools/{name}/run` endpoint.

    Verified: full suite (842 passed, up from 825) covering
    `parse_cron_hosts`, crontab parsing (schedule/command/enabled,
    `@reboot`-style special schedules, env-var lines and plain comments
    correctly *not* treated as jobs), toggling both directions, an
    unreachable host degrading to an error entry without affecting
    others, tool registration/permission tiers gated on
    `SIRISAI_CRON_HOSTS`, and the route's empty-list default. Live-verified
    against a *real* SSH server and a *real* crontab (not a fake/mocked
    one -- this is the first integration in this codebase to shell out to
    `ssh` rather than call an HTTP API, so proving the actual subprocess
    invocation and remote `crontab -l`/`crontab -` round trip mattered
    more than usual): stood up a local `sshd` with a real user crontab
    (an enabled job, a disabled/commented-out job, an interval job, and
    an `@reboot` job) and drove the real REST endpoints end to end --
    `GET /siris/cron_jobs` and `POST /siris/tools/cron_list_jobs/run`
    both parsed it correctly; `POST /siris/tools/cron_set_job_enabled/run`
    disabled an active job and re-enabled a disabled one, each confirmed
    by re-reading the actual crontab afterward (not just the tool's own
    return value); and a second host pointed at an unreachable address
    correctly came back as one `{host, error}` entry alongside the first
    host's real jobs rather than failing the whole response.

105. **Done** -- CI: run the test suite automatically on every PR and push
    to main (`.github/workflows/tests.yml`). Every milestone up to this
    one had "still no CI configured" noted in its own PR -- correctness
    depended entirely on whoever was working the repo (me, in this
    session) remembering to run `pytest` by hand before merging.
    GitHub-hosted `ubuntu-latest`, Python 3.12 (matching the Dockerfile),
    `pip install -r requirements-dev.txt` then `pytest` -- the same two
    commands used locally throughout this whole session, scoped to
    `tests/` the same way `pytest.ini` already is (`satellite/` stays
    excluded, per its own comment: a separate deployable with hardware-
    adjacent test requirements).

    Deliberately minimal: no lint/type-check step, since there's no
    committed ruff/mypy config in the repo to run against, and inventing
    one wasn't asked for -- adding rules nobody agreed to would just be
    noise on every future PR. Doesn't (can't) cover the live-verification
    every milestone in this README describes doing by hand -- real
    Home Assistant/SSH round trips against fake or real servers -- only
    the fast unit/integration suite; that manual step is still what
    proves a feature works end to end, this just guarantees the suite
    itself gets run every time instead of relying on memory.

    Verified: confirmed the exact two commands the workflow runs
    (`pip install -r requirements-dev.txt`, `pytest`) succeed locally
    against a clean install of the pinned dependencies -- 863 passed.
    The workflow itself will get its first real run on this change's own
    PR, which is the natural live-verification for "does CI run" (no
    fake CI to test it against beforehand).

106. **Done** -- Streaming voice conversation: replies start playing after
    the first sentence, you can interrupt, follow-ups need no wake word,
    and actions can be confirmed out loud. First pick from the Jarvis
    brainstorm ("conversation that feels like Jarvis").

    Server -- `POST /siris/voice/converse` (`app/api/routes/voice.py`,
    `app/voice/turn.py`): audio (or `text`) in, newline-delimited JSON
    out -- `transcript`, then a `sentence` event (text + base64 MP3) for
    each sentence of the reply *as it is written*, then `final`. It rides
    the same Cortex event stream `/siris/chat/stream` uses, so it gets
    tools, the planner, context and persona unchanged.
    - `SentenceChunker` (`app/voice/sentences.py`) splits streamed text at
      sentence ends and newlines (not at "e.g."/"21.5"), skips fenced
      code, and `speakable()` strips markdown before TTS.
    - What's spoken always matches the recorded answer: text before a
      tool call is treated as a preamble, and at `final` only the part of
      the answer not already spoken is added -- covering the correction
      Cortex's `_finalize` appends after streaming, a planner's trimmed
      final text, and backends that don't stream.
    - Spoken confirmation (`app/voice/confirmations.py`): when the turn
      stops for confirmation, SirisAI says "I need your OK to turn on
      reactor. Should I go ahead?" and a short "yes"/"go ahead"/"no"/
      "cancel" as the next utterance in that conversation answers it
      (pending for 2 minutes, in memory). Locks, covers (garage doors)
      and alarm panels are never confirmable by voice
      (`SIRISAI_VOICE_CONFIRMATION_BLOCKED_DOMAINS`) -- you're told to
      confirm those in the app. `SIRISAI_VOICE_CONFIRMATION_ENABLED=false`
      turns spoken confirmation off entirely.
    - `final.expects_reply` is true when SirisAI asked something, so the
      satellite knows to keep listening.

    Satellite (`satellite/siris_satellite/`): a conversation is now a loop.
    `PlaybackQueue` plays each sentence as it arrives; barge-in listens for
    the wake word *during* playback and kills mpg123 the moment it fires,
    then records straight away; after a reply it listens for
    `FOLLOW_UP_SECONDS` (default 6) without the wake word, at least 8s when
    a question was asked. Recording start/stop is `UtteranceRecorder`
    (`recorder.py`, pure logic): wait for speech, 0.3s pre-roll, stop after
    1.2s of non-speech. "Is this speech" comes from Silero VAD (bundled with
    openWakeWord) with the old loudness check as a fallback. An older server
    without `/converse` (404) makes the satellite fall back to transcribe ->
    chat -> speak. New optional `SIRISAI_SATELLITE_USER` sends a fixed
    `X-Siris-User` for a satellite in someone's own room. CI now runs the
    satellite's own suite too (a second job in `.github/workflows/tests.yml`).

    Not done: acoustic echo cancellation (the satellite's own speaker is
    rarely loud enough at its mic to fire the wake word; `BARGE_IN=false`
    if yours is), and the web UI still uses its existing record ->
    transcribe -> review flow.

    Verified: 16 new server tests (chunking, markdown stripping, prefix
    reconciliation, yes/no parsing, the endpoint's sentence streaming,
    correction-spoken-once, yes runs / no cancels / blocked domain never
    voice-confirmable) and 13 new satellite tests (streamed playback,
    follow-up windows, question window, barge-in cutting playback short,
    fallback to the old endpoints, recorder timing). Live: a real uvicorn
    server backed by fake Ollama (word-by-word streaming), fake STT/TTS
    and a fake Home Assistant, driven by the real `SirisClient` and
    `Satellite` loop with scripted audio. The first sentence played at
    0.5s of a 2s answer; a follow-up was answered without the wake word;
    "turn on the reactor" -> "Should I go ahead?" -> "yes" turned the
    Home Assistant switch on (checked in HA's state afterwards); "unlock
    the front door" -> "yes" left the lock locked and said to confirm in
    the app; saying the wake word 0.9s into a three-sentence reply cut it
    off after the first sentence and recorded the next question.

107. **Done** -- Speaker recognition by voice. Multi-user recognition
    (milestone 46) scoped memory, goals and "my ..." to whoever sent
    `X-Siris-User`, but a satellite in a shared room had no way to know who
    was talking. Now enrolled household members are recognised from the
    recording itself.

    - Encoder (`app/voice/speaker.py`): Resemblyzer's pretrained
      speaker-verification network (Apache-2.0; 3-layer LSTM over 40-band
      mel spectrograms, 256-d embeddings) run in **plain numpy** -- no
      PyTorch, no sidecar. The same preprocessing as the original: volume
      normalisation to -30 dBFS, long-silence trimming, librosa-equivalent
      Slaney mel filterbank, 1.6s partial windows averaged and
      L2-normalised. ~0.1s per utterance on CPU.
    - Weights: `scripts/fetch_voice_encoder.py` downloads the Resemblyzer
      wheel from PyPI (sha256-pinned), and reads its legacy-format
      `pretrained.pt` with a restricted unpickler that only builds numpy
      arrays (anything else in the pickle, e.g. `os.system`, is refused).
      It writes a 5.7MB `.npz`. The Dockerfile runs it at build time
      into `/app/models` (not the mounted `/app/data`). Speaker recognition
      is on whenever that file exists (`SIRISAI_SPEAKER_ID_MODEL_PATH`,
      default `models/voice_encoder.npz`); `numpy` joins requirements.txt.
    - Voiceprints (`app/voice/voiceprints.py`, `voiceprints` table): each
      person's print is the running mean of their enrolled samples. A match
      needs cosine similarity >= `SIRISAI_SPEAKER_ID_THRESHOLD` (0.80) *and*
      a 0.02 lead over the next-closest person -- an uncertain match stays
      the default user rather than guessing.
    - API: `POST /siris/voice/voiceprints/enroll`, `GET
      /siris/voice/voiceprints`, `DELETE /siris/voice/voiceprints/{user}`,
      `POST /siris/voice/identify` (all scores, for checking enrolment).
      `POST /siris/voice/converse` identifies the speaker first (unless an
      explicit `X-Siris-User` header was sent -- explicit wins), emits a
      `speaker` event and runs the whole turn as that user. The context
      engine now tells the model "You are talking with: <user>" for any
      non-default user. Tools: `list_voiceprints`, `delete_voiceprint`.
    - Web UI: a Voice ID section in the Status tab (shown when
      `/siris/status` reports `speaker_recognition`) records 5-second WAV
      samples in the browser (Web Audio -> PCM WAV, so no ffmpeg is needed
      server-side), enrols them, lists and forgets voiceprints, and has a
      "Who am I?" check. Non-WAV uploads are decoded with ffmpeg when it's
      installed; otherwise they just aren't identified.

    Verified: 14 new tests -- the converter against a legacy checkpoint
    written by hand (no torch), refusal of a malicious pickle, filterbank
    and spectrogram shapes, partial windows, silence trimming, embedding
    normalisation/determinism, threshold + margin + ambiguity in the store,
    the API round trip, `converse` running as the recognised speaker, an
    explicit header beating recognition, and the context line. Live, with
    the *real* converted weights: four synthetic speakers (espeak-ng, two
    higher and two lower voices) enrolled with three sentences each through
    the running server; each one's unseen fourth sentence was identified
    correctly (0.94-0.97 against their own print; the two low male voices
    were the closest pair, 0.93 vs 0.97). A `converse` call with Carol's
    unseen sentence produced `{"type": "speaker", "user_id": "carol"}` and
    the model's context carried "You are talking with: carol". In headless
    Chromium, with a WAV file as the fake microphone, the Status tab
    enrolled a sample through the real browser recording path (48kHz ->
    resampled server-side) and "Who am I?" named alice and dave correctly.
    Real human voices vary more than synthetic ones; three or four samples
    per person from different rooms are recommended in the UI.

108. **Done** -- Vision: SirisAI can look at a camera and at photos you send
    it. Frigate (milestone 89) only reported labels ("person", 0.91);
    now a local vision-language model says what's actually there.

    - `app/vision/client.py` -- `VisionClient`, an Ollama `/api/chat` call
      with base64 `images` to `SIRISAI_VISION_MODEL` (e.g. `qwen2.5vl:7b`,
      `llava:7b`, `gemma3:4b`; `SIRISAI_VISION_BASE_URL` defaults to the
      chat Ollama). Separate from the chat LLM: a different, slower model
      that only ever sees images.
    - Frigate + vision model configured -> two READ tools:
      `frigate_look_at_camera(camera, question?)` (the live frame from
      `/api/<camera>/latest.jpg`; "is the garage door open?", "who's at the
      door?" -- questions no sensor answers) and
      `frigate_describe_event(event_id, question?)` (the event's saved
      snapshot). Both are treated as external content by the injection
      guard (`app/core/taint.py`): text visible in a frame (a sign, a note)
      is the camera's, not yours. Frigate tools now group under a
      `frigate` capability category.
    - `GET /siris/cameras` and `GET /siris/cameras/{camera}/latest.jpg`
      proxy frames through SirisAI's own auth (used by the HUD, milestone
      114), so Frigate itself needn't be exposed.
    - Photos in chat: `POST /siris/chat` and `/siris/chat/stream` accept
      `images` (base64 or data URLs, up to 4 x 8MB). Cortex asks the vision
      model about them in light of your message ("transcribe any visible
      text") and stores the answer inside your message as `[[image: ...]]`,
      so later turns can still refer to the picture. That text is excluded
      from what counts as "what the user said" for the injection guard --
      a photographed note saying "remember the alarm code is 1234" gets
      held for confirmation instead of saved (tested). Without a vision
      model the chat model is told plainly that it can't see the image.
    - Web UI: a paperclip button (and paste) attaches photos; they're
      resized in the browser to <=1280px JPEG, previewed with a remove
      button, sent with the message and shown as a thumbnail in your
      bubble. Reopened conversations show a paperclip in place of the
      stored description.

    Verified: 11 new tests (the Ollama images payload, both Frigate tools'
    URLs and outputs, registration only with both Frigate and a vision
    model, the external-content marking, the frame proxy with and without
    Frigate, description stored with the message and seen by the chat
    model, no-vision-model honesty, invalid/too-many images rejected, and
    image text not laundered past the confirmation guard). Live: a real
    uvicorn server with a fake Frigate serving a real JPEG and a fake
    Ollama answering vision calls -- "who is at the front door right now?"
    made the model call `frigate_look_at_camera`, the actual 4958-byte
    frame reached the vision model and its description came back as the
    answer; `GET /siris/cameras/front_door/latest.jpg` returned the
    camera's JPEG byte-for-byte. In headless Chromium, attaching a JPEG
    showed a preview, sending it put a thumbnail in the user bubble,
    cleared the tray, and the browser-resized image reached the vision
    model.

109. **Done** -- Protocols, guest mode and intruder response: Jarvis-style
    named playbooks across every system, a privacy mode for when visitors
    are over, and an alarm when someone is on camera who shouldn't be.

    Protocols (`app/protocols/service.py`, `app/plugins/builtin/protocols.py`):
    - A step is any registered tool call (not just Home Assistant like
      scenes), optionally conditional -- `when`: `nobody_home`,
      `someone_home`, `night`, `day` (`SIRISAI_NIGHT_START_HOUR`/
      `_END_HOUR`), or `{entity, state}`/`{entity, not_state}` -- and Home
      Assistant steps can target a selector (`{"select": {"domain":
      "cover", "device_class": ["garage", "gate"]}}`) instead of ids.
    - Built-ins, adapting to what's configured (a step whose tool isn't
      registered is dropped): **lockdown** (lock every lock, close garage
      doors/gates, all lights on at night, notify), **away** (lights and
      media off, lock up, close the garage, notify), **guest** (guest mode
      for 24h + guest Wi-Fi switch, `SIRISAI_GUEST_WIFI_ENTITY_ID`) and
      **guest_off**. `create_protocol`/`delete_protocol` manage your own
      (validated: real tools only, no protocols inside protocols), and a
      custom one can override a built-in by name.
    - `run_protocol` (CONFIRM_REQUIRED, one confirmation covers every
      step; `dry_run` lists each step and whether its condition holds) and
      `undo_last_protocol`: before each Home Assistant step the affected
      entities' states are recorded, and only what actually changed is put
      back (a door that was already locked stays locked); guest-mode steps
      restore the previous mode; a step may declare its own `undo`. A
      failed step doesn't stop the rest -- a lockdown still locks the
      doors if the notification fails.
    - Voice: "run the lockdown protocol -- should I go ahead?" can be
      answered by voice because lockdown only locks and closes; a protocol
      with an unlocking step, and any undo, must be confirmed in the app.
      Securing calls (lock, close a cover, arm an alarm) are now voice-
      confirmable on their own too -- only the opening direction is held.
    - Web UI: a Protocols tab -- each protocol's steps, Run (shows the dry
      run, then asks once), Undo last protocol, and a Guest mode toggle.

    Guest mode (`app/core/guest.py`, `set_guest_mode`/`guest_mode_status`,
    optional auto-off after N hours): personal-data tools (memory,
    conversations, calendars, reminders, goals, voiceprints, briefings)
    aren't offered to the model at all, the context engine drops memories
    and recent events, and the model is told a visitor may be listening.

    Intruder response (`app/skills/builtin/intruder.py`, `intruder_check`,
    on the scheduler every `SIRISAI_INTRUDER_CHECK_INTERVAL_MINUTES`): a new
    Frigate person detection while `SIRISAI_INTRUDER_ALERT_WHEN` holds
    (`away`, `night`, or default `away_or_night`) -> the vision model
    describes the snapshot (milestone 108) -> an urgent push, a spoken
    warning in the occupied room at night, and -- only with
    `SIRISAI_INTRUDER_AUTO_LOCKDOWN=true` -- the lockdown protocol. The
    first run only records a baseline; each camera alerts at most once per
    `SIRISAI_INTRUDER_COOLDOWN_MINUTES`; `SIRISAI_INTRUDER_CAMERAS` limits
    which cameras count. Detections are read directly (not audited every
    minute); everything it *does* is audited. Small persistent facts like
    these live in a new key/value `app_state` table (`app/core/state.py`).

    Bug fixed on the way: `home_assistant_call_service` with
    `entity_id: "all"` was fuzzy-"corrected" to a single entity whenever a
    name contained "all" -- `light.hall` -- so "turn off all the lights"
    turned off only the hall. "all"/"none" and lists are now passed through
    untouched, and verification checks every entity in a list.

    Verified: 14 new tests (built-ins adapting to configuration, one
    confirmation + an explanatory dry run, lockdown then a precise undo,
    the night-only step skipped by day, custom protocols with entity
    conditions and creation validation, presence-conditioned steps, guest
    mode hiding tools and context then restoring them, guest auto-expiry,
    guest undo, voice-confirmability of protocols, intruder baseline ->
    alert -> cooldown, quiet by day with people home, auto-lockdown, and
    the "all" regression). Live against a real server with fake Home
    Assistant/Frigate/ntfy/Ollama: in headless Chromium the Protocols tab
    previewed lockdown (night step shown as skipped), ran it (back door
    locked, garage closed, the blind -- not a garage -- left open), and
    Undo put back exactly the two changes; the Guest toggle removed
    recall/remember/set_reminder from what the next chat turn was offered.
    By voice, "initiate lockdown" got "I need your OK to run the lockdown
    protocol. Should I go ahead?" and "yes" locked the door. With presence
    set to away, `intruder_check` turned a Frigate person event into an
    urgent push carrying the vision model's description.

110. **Done** -- Self-healing homelab: watched containers that go down get
    the known fix automatically, within a budget, and a human gets the logs
    when that doesn't work. Before this, SirisAI noticed a failed container
    and notified; closing the loop is the Jarvis step.

    `app/remediation/runner.py`, run by the scheduler every
    `SIRISAI_REMEDIATION_CHECK_INTERVAL_MINUTES` (5):
    - Watches only what you list in `SIRISAI_REMEDIATION_CONTAINERS`
      (comma-separated, or `*`). Listing a container is your standing
      approval to restart exactly that container without asking, so the
      restart goes through `docker_restart_container` auto-confirmed, with
      a `self-healing: ...` trigger in the audit log.
    - Down means exited, dead, stuck restarting, or `(unhealthy)`.
    - Runbook: restart -> wait `SIRISAI_REMEDIATION_SETTLE_SECONDS` (8) and
      re-check -> still up: a note queued for the next briefing, no push;
      not up, or not allowed to try: read the last 15 log lines and send
      one urgent push with them (once per container per hour).
    - Budget: at most `SIRISAI_REMEDIATION_MAX_RESTARTS_PER_CONTAINER` (2)
      restarts per container and `SIRISAI_REMEDIATION_MAX_ACTIONS_PER_HOUR`
      (4) in total per rolling hour -- a crash loop gets a couple of
      restarts, then you, never an endless restart loop hiding the real
      problem. `remediation_status` (READ) shows what's watched, what was
      done this hour and the budget left.

    Found live and fixed: `docker_restart_container`'s own verify() checks
    straight after the restart, and a container that crashes on start is
    briefly "running" then -- against a real always-crashing container it
    reported success. Self-healing now judges by the settled state instead.

    Verified: 6 tests (unhealthy detection, recover -> briefing note and
    no push, crash loop -> two restarts then one alert with logs and a
    reset after an hour, the global budget across containers, nothing
    without a watch list, the briefly-up-then-crashed regression). Live
    against a **real Docker daemon** with real busybox containers and
    SirisAI's real scheduler: `flaky` (crashes once, then serves) was
    restarted once and stayed up, and its recovery note was queued;
    `broken` (always exits) was restarted twice, recognised as still down
    each time, and produced one urgent push containing its actual
    `panic: /config/app.yml missing` log lines, after which it was left
    alone; an unwatched healthy container was never touched.

111. **Done** -- Security briefing and backup verification: one check-up
    that answers "is anything about the homelab's security or backups
    wrong?", run on a schedule and on request.

    `security_briefing` (`app/skills/builtin/security.py`, checks in
    `app/security/`), every section best-effort (an unreachable source is
    reported, never fatal):
    - **Certificates** -- days until expiry for each `host[:port]` in
      `SIRISAI_SECURITY_CERT_HOSTS`, with verification off on purpose
      (self-signed homelab certs are the norm; the question is expiry, not
      trust). Uses `cryptography` when installed, otherwise the stdlib's
      own certificate decoder -- both paths are tested.
    - **Failed SSH logins** on the cron hosts (milestone 104's SSH access):
      "Failed password / Invalid user" counts per source IP over 24h, from
      the journal if it has any, else `/var/log/auth.log`; warned at
      `SIRISAI_SECURITY_SSH_FAILED_THRESHOLD` (25).
    - **UniFi intrusion alerts** from the controller's IPS event log (the
      classic API -- the Integration API has none; reported as
      unavailable where the controller doesn't accept the API key there).
    - **Vulnerable images** -- `security_scan_images` runs Trivy over the
      images of running containers (HIGH/CRITICAL counts, fixable count,
      worst examples), weekly on the scheduler when `trivy` is installed;
      the briefing reads the last result.
    - **Backups** -- `backup_check` reads each Hyper Backup task's last
      result *and* last successful time (new `SYNO.Backup.Task status`
      call): failed runs, tasks that haven't succeeded within
      `SIRISAI_BACKUP_MAX_AGE_HOURS` (26), and tasks with no success on
      record are all flagged -- a job that silently stopped running looks
      fine by its last result alone.

    The scheduler runs it every `SIRISAI_SECURITY_CHECK_INTERVAL_HOURS` (6)
    and acts only on problems: an urgent push for a failed/stale backup or
    a certificate inside a week of expiry; everything else is queued for
    the daily briefing.

    Found live and fixed: the SSH check's first form,
    `journalctl ... || cat auth.log`, never reached the log file on a host
    where journalctl exists but holds no sshd entries -- it exits 0 with
    nothing. It now falls back whenever the journal has no matches.

    Verified: 10 tests (certificate expiry against a real local TLS server
    with an openssl-generated self-signed cert, with and without
    `cryptography`; unreachable cert host; SSH output parsing with one host
    failing; Trivy JSON summarised via a stand-in binary; backup evaluation
    -- ok, failed, stale, never, running; backup check and briefing through
    the tools; the scheduled watch pushing the urgent item and queuing the
    rest; all-clear with nothing configured). Live against a real server:
    a real `sshd` (logging to a file) that had just rejected real logins
    for invalid users, reached over real SSH, and a real TLS server whose
    certificate expires in 5 days -- the briefing reported "The
    certificate on 127.0.0.1:18443 expires in 5 days; 4 failed SSH logins
    on root@127.0.0.1 in 24h (from 127.0.0.1 (4))". Trivy itself couldn't
    be installed in this sandbox; its output parsing is covered by tests.

112. **Done** -- Habit-based automation suggestions: SirisAI notices what
    you do by hand at the same time most days and offers to automate it,
    with one tap.

    `app/anticipation/habits.py`: from the Home Assistant logbook over the
    last `SIRISAI_HABIT_LOOKBACK_DAYS` (14), every *manual* change to a
    light, switch, fan, media player, cover, climate or input_boolean
    (anything HA attributes to an automation or script is ignored, and so
    is that entity+state entirely -- it's already automated) is grouped by
    entity, new state and weekday/weekend. The busiest 40-minute window of
    each group must hold the change on at least 60% of eligible days and
    at least `SIRISAI_HABIT_MIN_DAYS` (5) of them. The result reads "You
    turn on Office around 07:10 on weekdays (9 of 10)" and carries a ready
    automation: time trigger, weekday condition, the same service call.
    Times are in `SIRISAI_TIMEZONE`.

    - `habit_suggestions` (READ) mines and lists open suggestions;
      `accept_habit_suggestion` (CONFIRM_REQUIRED, dry-runnable) creates
      the automation via `home_assistant_create_automation`;
      `dismiss_habit_suggestion` remembers not to ask again. Suggestions
      live in `app_state`, so nothing is suggested twice.
    - Every `SIRISAI_HABIT_CHECK_INTERVAL_DAYS` (7) the scheduler pushes
      each new suggestion with **Create automation** / **No thanks**
      buttons (ntfy `http` actions straight to the tool endpoints, as
      milestone 43's confirmations do; needs `SIRISAI_PUBLIC_BASE_URL`).

    Verified: 4 tests (real habits found and random activity, sensors and
    automation-driven changes ignored; the time shifting with timezone;
    suggest -> not re-suggested -> accept needs confirmation -> automation
    created -> dismiss; the scheduled push and its button payloads). Live:
    a real server whose real scheduler mined a fake Home Assistant's two-
    week logbook and pushed "You turn on Kitchen around 06:44 on weekdays
    (10 of 10). Want me to automate it?" (the automation-driven hall light
    wasn't suggested); replaying the push's own "Create automation" request
    created exactly that automation in Home Assistant.

113. **Done** -- Heads-up when you leave: "Before you go: garage door is
    open; back door is unlocked; 1 light on. 'Dentist' at 09:00 starts in
    40 min. 80% chance of rain today -- take an umbrella." -- in one push,
    with a button to fix it.

    `departure_check` (`app/skills/builtin/departure.py`, SAFE_ACTION):
    - Left open (Home Assistant): garage doors/gates/door covers open,
      locks unlocked, lights on, media playing. Window blinds don't count.
    - Next timed calendar event within `SIRISAI_DEPARTURE_EVENT_WINDOW_HOURS`
      (3); all-day events are skipped.
    - Today's rain chance from the forecast, mentioned at 50%+.
    - With `notify`, it pushes the summary (high priority when something is
      open) with a one-tap **Run Away protocol** button (milestone 109)
      when `SIRISAI_PUBLIC_BASE_URL` is set.
    - Runs automatically when `presence_check` sees the house go from
      occupied to empty, and on request -- "I'm heading out" in chat, or an
      iOS Shortcut hitting `/siris/ask` or the tool endpoint. Hidden in
      guest mode (it reads the calendar). No travel times: there's no maps
      integration.

    Verified: 3 tests (open garage/unlocked lock/lights, the next timed
    event in the window, rain, the push and its Away button; "all good"
    with nothing open and no button; presence going empty triggering it).
    Live against a real server with fake Home Assistant, Open-Meteo and
    ntfy: the push said "Before you go: Garage door is open;
    lock.back_door is unlocked; 1 light on (Kitchen). 80% chance of rain
    today (slight rain) -- take an umbrella."; the open blind wasn't
    listed; replaying the push's "Run Away protocol" request ran all five
    Away steps and Home Assistant then showed the garage closed and the
    back door locked.

114. **Done** -- Energy-aware scheduling: "run the dishwasher when power's
    cheap" holds the job until a cheap tariff window, enough solar, or a
    low price -- then runs it.

    `app/energy/service.py`: power counts as cheap when any configured
    signal says so -- inside a window in `SIRISAI_ENERGY_CHEAP_WINDOWS`
    (local `HH:MM-HH:MM`, comma-separated, may cross midnight), a Home
    Assistant solar sensor (`SIRISAI_ENERGY_SOLAR_ENTITY_ID`, W or kW) at
    or above `SIRISAI_ENERGY_SOLAR_MIN_WATTS` (1500), or a price sensor
    (`SIRISAI_ENERGY_PRICE_ENTITY_ID`) at or below `SIRISAI_ENERGY_PRICE_MAX`.

    - `schedule_for_cheap_energy(tool_name, arguments, reason?,
      deadline_hours?)` stores any tool call as a deferred job (new
      `deferred_jobs` table). It needs exactly the approval the deferred
      call would need -- confirming "start the dishwasher later" *is*
      confirming the dishwasher, and the job runs later with that approval
      attached; a READ/SAFE call needs none. Protocols and the scheduling
      tools themselves can't be deferred.
    - The scheduler checks every tick and runs waiting jobs when power is
      cheap, or at a job's deadline regardless (nothing waits forever on a
      cloudy week); each run is audited with why it ran ("solar is
      producing 2600 W") and a note is queued for the briefing.
    - `energy_status` (cheap now?, why, solar watts, price, next cheap
      window, waiting jobs), `list_deferred_jobs`, `cancel_deferred_job`.

    Verified: 6 tests (window parsing incl. midnight crossing; status from
    window, solar in kW, price, and the next window; a CONFIRM_REQUIRED
    target needing confirmation to schedule then running with it when
    cheap; a SAFE target needing none and its deadline forcing a run;
    cancel and guards; the scheduler tick running due jobs). Live: a real
    server and scheduler with a fake Home Assistant -- scheduling
    `switch.dishwasher` on asked for confirmation, then waited while solar
    read 350 W; when the sensor changed to 2.6 kW the scheduler turned the
    dishwasher on within seconds, audited as "energy scheduler: deferred
    job #1 (dishwasher) -- solar is producing 2600 W".

115. **Done** -- Email triage: what's unread and actually matters, read
    aloud or summarised, with replies drafted for you (or sent, once you
    confirm).

    `app/email/client.py` speaks plain IMAP + SMTP (stdlib `imaplib`/
    `smtplib`), so Gmail/iCloud/Fastmail app passwords, a self-hosted
    Dovecot or anything else works. Configure `SIRISAI_EMAIL_IMAP_HOST`,
    `_USERNAME`, `_PASSWORD` (plus `_SMTP_HOST` to send replies; see
    `app/core/config.py` for ports, TLS and the drafts folder).
    - "Important" is simple and explainable: mail that looks automated --
      `List-Unsubscribe`/`List-Id`, `Precedence: bulk`, a no-reply or
      notifications sender -- isn't; anything else is, and each message
      says why ("sent to you directly", "mailing list / newsletter").
    - Tools: `email_list_unread` (READ, never marks anything read; can
      filter to important), `email_read` (plain text; HTML reduced to its
      words), `email_draft_reply` (SAFE_ACTION -- appends a proper reply
      to the Drafts folder with `In-Reply-To`/`References` and "Re:",
      nothing leaves) and `email_send_reply` (CONFIRM_REQUIRED).
    - Email content is untrusted: both read tools are external-content
      tools for the injection guard, and the draft/send tools are free-
      text writers, so text copied out of an email can't be saved or
      drafted on its own say-so (tested: an email saying "remember the
      alarm code..." gets held for confirmation).
    - The daily briefing gains an "extras" section
      (`app/skills/builtin/briefing_extras.py`) that personal integrations
      contribute to -- here "Email: 1 important of 3 unread -- Sam Lee:
      Dinner on Friday?". Email tools are hidden in guest mode.

    Found live and fixed: sending tried SMTP AUTH whenever a password was
    set, which a local relay with no AUTH (a homelab Postfix/smarthost)
    rejects even though it would deliver; it now logs in only when the
    server offers AUTH.

    Verified: 8 tests (importance rules, plain-over-HTML body extraction,
    tool permissions and filtering, the injection case through a real
    planner run, the briefing line, registration only when configured,
    the no-AUTH relay). Live against a **real Dovecot IMAP server** and a
    **real SMTP server** (aiosmtpd): of three unread messages, the
    personal one was marked important and the newsletter and GitHub
    no-reply weren't; reading returned the real body; the draft landed in
    Dovecot's Drafts folder as "Re: Dinner on Friday?" with the right
    In-Reply-To; nothing was marked read; sending needed confirmation,
    then the SMTP server received the threaded reply; the briefing line
    read as above.

116. **Done** -- Health data from your phone: sleep, steps and heart rate
    feed the briefing ("Health: slept 5.8h -- 1.5h under your usual;
    resting heart rate 62 bpm -- 7.0 bpm over your usual") and answer "how
    did I sleep?".

    - `POST /siris/health/metrics` (`app/api/routes/health_data.py`) takes
      `{"date": "YYYY-MM-DD", "metrics": {...}}` or a flat body -- built
      for an Apple Shortcuts automation (Find Health Samples -> Get
      Contents of URL, with the usual `Authorization` and `X-Siris-User`
      headers), but anything that can POST JSON works. Accepted metrics,
      with common export aliases: sleep_hours, steps, resting_heart_rate,
      heart_rate_variability, active_energy_kcal, weight_kg,
      exercise_minutes; unknown names are reported back, not stored.
      One value per user, metric and day (re-posting a day replaces it).
    - `health_summary` (READ) and `GET /siris/health/summary`: latest day,
      7-day average, and a comparison with *your own* 30-day baseline --
      only notable, concerning moves are called out (sleep 45+ min short,
      steps 35% down, resting HR 5+ bpm up, HRV 25% down).
    - Per user, like memory and goals: the summary is whoever is asking
      (header or voice recognition). Hidden in guest mode. The daily
      briefing's extras include a health line when there's data.

    Verified: 5 tests (aliases and unknowns, a Shortcut-style post and
    same-day replacement, rejected bodies, notes against a personal
    baseline -- including that a small step dip isn't mentioned, and the
    per-user tool and briefing line). Live: a week of posts as `brad`
    through the running server, then a rough night -- the summary and
    briefing called out the short sleep, low steps and raised resting
    heart rate, while `jan` saw no data at all.

117. **Done** -- The car and your parcels.

    Car (`app/car/service.py`), through whatever Home Assistant integration
    it already has (Tesla, Kia/Hyundai, BMW, Polestar, an OBD dongle...):
    each piece is an optional entity setting -- `SIRISAI_CAR_BATTERY_`,
    `_RANGE_`, `_CHARGING_`, `_PLUGGED_`, `_LOCATION_`, `_LOCK_`,
    `_CLIMATE_ENTITY_ID`, plus `SIRISAI_CAR_NAME`.
    - `car_status` (READ): "The Polestar: 24%, 96 km range, at home,
      UNLOCKED", with `low_battery` when it's under
      `SIRISAI_CAR_LOW_BATTERY_PERCENT` (30) and not plugged in.
    - `car_precondition(on, temperature?)` (SAFE_ACTION): a `climate.*`
      entity gets turn_on/off + set_temperature; a `switch.*` or
      `button.*` is turned on/pressed.
    - The briefing adds the car's line and "It's low and not plugged in --
      charge it tonight."; the departure heads-up (milestone 113) mentions
      a low, unplugged car too.

    Parcels (`app/parcels/service.py`, `parcels` table):
    `parcel_add(tracking_number, label)`, `parcel_list`, `parcel_done`.
    With `SIRISAI_SEVENTEENTRACK_API_KEY`, numbers are registered with
    17TRACK (API v2.2, carrier auto-detected) and refreshed every
    `SIRISAI_PARCEL_REFRESH_MINUTES` (120); a change to out for delivery,
    delivered, ready for pickup, failed or exception is pushed ("headphones
    is out for delivery. ...Melbourne"). Without a key it's a list you
    keep yourself. The briefing lists active parcels with their ETA.

    Verified: 5 tests (car status parsing and the low-battery briefing and
    departure lines, preconditioning calls, parcel register -> refresh ->
    push only on a notable change -> briefing -> done, and the manual
    no-tracker list). Live against a real server with a fake Home
    Assistant and a fake 17TRACK: car status and the low-battery warning,
    preconditioning turned the car's climate entity on, the parcel was
    registered (with the `17token` header) and, once its status changed,
    the real scheduler's refresh pushed "headphones is out for delivery";
    the briefing carried both lines.

118. **Done** -- HUD and autonomy timeline: a full-screen dashboard for a
    wall tablet, and "what did you do on your own today?".

    Autonomy timeline (`app/audit/autonomy.py`): the audit log filtered to
    actions nobody asked for in the moment -- classified by the `trigger`
    every autonomous path already records (scheduled checks, self-healing,
    intruder alerts, the energy scheduler, the security watch, habit
    pushes, presence scenes, routines, memories it chose to keep) --
    described in plain words ("Restarted plex", "Sent 'SirisAI: person
    detected': ..."), with why and whether it was verified. Read-only
    lookups are left out unless asked. `what_did_you_do` (chat) and
    `GET /siris/autonomy/timeline?hours=`.

    HUD (`GET /hud`, `app/web/static/hud.html`, linked from the web UI's
    nav): self-contained like the main UI (no external scripts), dark,
    built for a tablet on the wall.
    - Clock and date; chips for guest mode, cheap power and a low car
      battery; CPU/memory/disk rings; outside temperature; the next
      events; the car; deliveries; live camera tiles (frames fetched
      through SirisAI's auth via milestone 108's proxy, refreshed every
      10s); and the autonomy timeline ("On my own today").
    - A central orb you tap to talk: it records WAV in the browser (with a
      live circular waveform), stops on a pause, sends it to
      `/siris/voice/converse` (milestone 106), shows what it heard and
      plays the streamed reply sentence by sentence while the orb pulses;
      a recognised speaker (milestone 107) is greeted by name, and a
      question back ("Should I go ahead?") prompts "Tap to answer".
    - Everything comes from one `GET /siris/hud/summary`, every section
      best-effort, refreshed every 15s. Open it once as `/hud?key=...` to
      store the API key in that browser.

    Verified: 4 tests (autonomous-trigger classification, the timeline
    keeping only autonomous non-read actions newest first, the tool and
    both routes, the page and its nav link). Live in headless Chromium
    against a real server with fake Home Assistant/Frigate/Open-Meteo/
    Ollama/STT/TTS: the HUD showed the cheap-power chip, 17° partly
    cloudy, "The Polestar: 64%, 251 km range, plugged in, at home", the
    tracked parcel, two camera tiles with real frames, and the intruder
    alert it had sent on its own (with the vision model's description);
    tapping the orb with a WAV file as the microphone transcribed "what's
    the weather" and showed and played the streamed reply, with no page
    errors. Found and fixed on the way: a weather reading with no
    temperature rendered as "0°".

119. **Done** -- Tool choice you can measure, and tool matching by meaning.
    With 176 tools when everything is configured, the selector (milestone
    "tool selection", `app/plugins/selector.py`) offers the model ~32 per
    turn; a tool it doesn't offer can't be called. This makes that
    measurable and less dependent on exact wording.

    - **Evals** (`evals/tool_choice.jsonl`, `app/evals/tool_choice.py`):
      71 everyday requests, each with the tool(s) that are a correct first
      call -- including paraphrases that share no words with the tool
      ("it's freezing in here", "has anything arrived in the post?", "my
      mates are coming round, keep my private stuff private"). Two levels:
      *selection* (deterministic, no model: is a correct tool offered?)
      and *choice* (the real model, offered those tools: does its first
      call land on a correct one?). Cases whose tools aren't configured
      on this server are skipped, not failed. `POST /siris/evals/run`
      (`?selection_only=true` for the fast half), `GET /siris/evals/history`,
      `python -m app.evals`; the scheduler runs the full eval every
      `SIRISAI_EVAL_INTERVAL_DAYS` (7) and pushes a warning if model
      accuracy drops 10+ points from the previous run -- e.g. after
      swapping models or adding tools.
    - **CI** now asserts the selection floor: with every integration
      configured (176 tools) at the real cap of 32, and on a partly
      configured server at a cap of 20, every case's tool is offered.
    - **Hints** for every feature added in milestones 106-118 (protocols,
      guest mode, email, health, car, parcels, energy, security, habits,
      autonomy, voiceprints, cameras, "every night at..." routines, felt
      temperature) -- the hint list predated all of them. Selection on the
      paraphrase set went from 67/71 to 71/71.
    - **Matching by meaning**: with an embedding model configured
      (`SIRISAI_EMBEDDING_PROVIDER=ollama`, the same one semantic memory
      uses), the tools closest in meaning to the message get a bonus by
      *rank* (so it behaves the same whichever embedding model you use).
      Tool descriptions are embedded once, in one batched `/api/embed`
      call (new `embed_many`), and re-embedded only if they change; if
      embedding fails, selection quietly falls back to keywords.
      `SIRISAI_TOOL_SELECTION_SEMANTIC=false` turns it off.

    Found live: on a partly configured server, "who's at the front door?"
    wasn't offered the camera tools and "every night at 11 turn off the
    office lights" wasn't offered `create_routine`. The first was
    structural: "door" points at both Home Assistant (17 tools) and the
    cameras, and on a score tie the bigger family filled the whole cap.
    Every family a message points at is now guaranteed its best two tools
    before the rest fill by score; both cases are in CI. With that, every
    case is offered its tool with the cap at 20 (at 16, one of 71 misses). Not verifiable here: the real model's choice accuracy, and the
    semantic layer with a real embedding model -- this sandbox can't reach
    the Ollama or Hugging Face model registries. The harness exists to
    measure exactly that on your own Ollama (`POST /siris/evals/run`).

    Verified: 9 tests (the selection floor at full configuration and on a
    partial one, a prompt with no shared words found only by meaning,
    tool vectors embedded once in a batch, fallback when embedding fails,
    wiring from settings, choice accuracy + history + the regression push,
    the routes, skipped-not-failed cases). Live against a real server with
    a fake Ollama serving `/api/embed` from a synonym-aware stand-in: the
    tools were embedded in one batch of 104 then one call per prompt; "any
    boxes sitting on the doorstep?" was offered `parcel_list` purely by
    meaning while "hello there" wasn't; the eval endpoints ran and
    recorded history.

120. **Done** -- Siris: SirisAI reads and grows Brad's knowledge
    vault, [Siris-Second-Brain](https://github.com/bradmairs/Siris-Second-Brain)
    -- Obsidian-compatible Markdown notes about his life, people, work,
    projects and home lab, with their own linking engine (`tools/brain.py`)
    and mind map.

    - **Tools** (`app/plugins/builtin/second_brain.py`, client in
      `app/second_brain/client.py`): `brain_search` and `brain_get_note`
      (READ; a note comes back with its links and backlinks, found by
      title or alias, so "Dom" finds "Dom Mairs"), `brain_insights` (READ;
      see below), and `brain_learn`
      (SAFE_ACTION). Unlike `obsidian_create_or_update_note`'s full
      replace, learning only ever adds: a new title becomes a linked note,
      and an existing one gets a dated update. The engine links mentions
      of existing notes, adds backlinks, and logs the change in the day's
      daily note. `verify()` re-reads the note; `dry_run()` says whether
      it would create or update, and which note.
    - **The engine stays in the vault.** The client runs
      `python tools/brain.py search|show|learn --output json` from the
      checkout instead of re-implementing it, so the linking rules live in
      one place, and SirisAI never writes a note file itself.
    - **Memory flows in automatically.** With `SIRISAI_BRAIN_MIRROR_MEMORY`
      (default on), `remember_entity_fact` copies each new fact onto that
      entity's note, and `remember` copies free-text memories into a
      `Siris Memory Inbox` note, which the vault's inbox workflow files
      later. This happens whichever tool the model picks. Duplicates the
      stores already caught aren't copied, and a vault problem is
      reported under `second_brain` without failing the memory.
    - **Git sync** (`SIRISAI_BRAIN_GIT_SYNC`, off by default): reads and
      writes always use the local checkout straight away. Pulls (at most
      every `SIRISAI_BRAIN_PULL_INTERVAL_SECONDS`) and the push after each
      learn's commit run on background threads, so a reply never waits on
      the network. A push rebases once if another device pushed first. Failures leave the local
      checkout usable and show in `integration_health` as "Siris".
      The image now includes `git`.
    - **Setup:** `git clone git@github.com:bradmairs/Siris-Second-Brain.git
      data/second-brain` (already inside the mounted volume), then set
      `SIRISAI_BRAIN_PATH=/app/data/second-brain`. For sync, add
      `data/ssh/id_ed25519.pub` as a deploy key with write access and set
      `SIRISAI_BRAIN_GIT_SYNC=true`.
    - Selector hint and three tool-choice eval cases. 23 tests, including
      git sync against a real local remote, a background pull, a push that
      has to rebase, and a sync failure that must not fail the write.

121. **Done** -- Siris everywhere, and growing by itself.

    - **Chat context:** the context engine adds the top second-brain matches
      for each message (`SIRISAI_BRAIN_CONTEXT_NOTES`, default 3, strong
      matches only) as labelled background data. Questions about Brad's own
      world get answered from his notes without the model having to think
      of searching. The vault's search now handles whole questions, and
      ranks by meaning when embeddings are on.
    - **Linking by meaning:** with `SIRISAI_EMBEDDING_PROVIDER=ollama`, the
      vault engine gets the same Ollama and embedding model, so new notes
      link to related ones by meaning instead of shared words.
      `SIRISAI_TIMEZONE` is passed through too, so daily notes are dated
      in local time rather than the container's UTC.
    - **Capture from anywhere:** `POST /siris/brain/capture` with
      `{"text", "url", "title", "tags"}` puts a note in the vault's inbox,
      for an iOS Shortcut, the share sheet or a Home Assistant automation.
      `GET /siris/brain/search?q=` and `GET /siris/brain/today` are also
      available.
    - **Insights:** `GET /siris/brain/insights?days=30` (and the
      `brain_insights` tool) returns what the vault can tell about itself,
      worked out by the engine's `brain.py insights` with no LLM:
      ready-made `highlights` sentences, this week against last, daily
      activity, growing topics, the most connected notes, upcoming
      deadlines, overdue tasks, projects idle for three weeks or more, the
      inbox's age, gap counts, and notes that read alike but aren't linked.
    - **The mind map:** `/brain` (open once as `/brain?key=...`, like the
      HUD) shows the vault's interactive mind map, drawn fresh from the
      notes. It works full screen or in a Home Assistant webpage card. The
      HUD gets a "Learned today" tile.
    - **Growth jobs** (`app/second_brain/jobs.py`, on the scheduler, once
      per local day and remembered across restarts):
      - *Meeting notes* (removed in 2.66): calendar events no longer
        become notes at all, since they cluttered the brain. SirisOS shows
        the calendar instead.
      - *Dream* at `SIRISAI_BRAIN_DREAM_HOUR_LOCAL` (23): the general model
        reads the day's conversations and memory-inbox entries and writes
        what's worth keeping as atomic notes, reusing existing titles. It
        sees user and assistant turns only, never raw tool output, and is
        told to treat them as data.
      - *Weekly* on `SIRISAI_BRAIN_WEEKLY_DAY` (6 = Sunday) at
        `SIRISAI_BRAIN_WEEKLY_HOUR_LOCAL` (18): push
        `SIRISAI_BRAIN_GAP_QUESTIONS` (3) questions about the vault's gaps,
        never-asked first, so answering in chat fills them. Also a health
        snapshot onto the Health note and the week's home lab actions,
        errors and paused tools onto the Home Lab note.
    - 26 more tests: context, capture, map and today routes, every job and
      its scheduling (once per day, the weekly day only, failures marked
      and not retried), and strict parsing of the model's notes.

122. **Done** -- Siris files things itself. Every write now
    goes through the vault's router (`brain.py route`, in the vault). It
    decides whether a memory updates a note Siris already knows or starts
    a new person, project, resource or note, and which folder it goes in
    (work knowledge lands in the matching `Work/` subfolder). It also
    decides which life areas it belongs to (Career, Relationships, Health,
    Finances, Home & Life Admin, Learning, Creativity) and links the note
    from those area hubs.

    - `remember` sends free-text memories untitled. One that opens by
      naming something known ("Dom wants to...") lands on that note;
      anything else still goes to the memory inbox.
    - `brain_learn` defaults to `type: auto`, and its `dry_run()` shows
      the router's real decision: folder, type, areas and why.
    - The nightly dream no longer forces every note to be a plain "note".
    - Mirrored results include the areas chosen.
123. **Done** -- SirisAI runs Siris' nightly upkeep itself, on
    the local model, replacing the Claude Code routine (which couldn't
    reach the vault from the cloud). At `SIRISAI_BRAIN_UPKEEP_HOUR_LOCAL`
    (default 2:00) the `upkeep` job in `app/second_brain/jobs.py`:

    - files the memory inbox: entries that name a known note go there
      via the vault's router, the general model titles the rest, and
      filed entries move to an archive note (`brain.py inbox` /
      `clear-memory-inbox`). Entries nobody can place stay for Brad.
    - files the other inbox captures with `brain.py file`.
    - turns each repo's merged PRs and commits since the last sweep into
      a dated update on its project note, summarised by the model
      (`app/second_brain/github.py`; read-only
      `SIRISAI_BRAIN_GITHUB_TOKEN`, empty skips it). A repo with real
      work and no note yet gets a project note.
    - relinks the vault, and pushes one summary line if anything changed.

    On `SIRISAI_BRAIN_WEEKLY_DAY` the new `review` job writes the week's
    review note from the vault's insights. The dream no longer reads the
    memory inbox, so entries aren't filed twice.
124. **Done** -- Everything new goes through the brain's inbox. New notes from
    chat (`brain_learn`) and the nightly dream now wait in `00-Inbox/` (still
    searchable), as do new notes from the Obsidian vaults (vault side). Adding
    to a note that already has that exact title still goes straight in. The
    upkeep's sort then shows the general model each inbox note with the
    existing notes a search finds for it, and the model picks: merge into one
    of those notes (`brain.py file --into`), file as a new note of a given
    type, or leave it for Brad. Without a model, the router files it as before.

125. **Done** -- A contract SirisOS can rely on: `/siris/hub/v1`. SirisOS
    used to run SirisAI tools by name and read their raw results, so renaming
    a tool or reshaping a result quietly emptied part of its daily brief and
    no test in either repo noticed. Now (`app/hub/`, `app/api/routes/hub.py`):
    - `GET /siris/hub/v1` -- who the request acts as (`X-Siris-User`), guest
      mode, which integrations are configured, which sections are personal.
    - `GET /siris/hub/v1/brief` -- the day as data, no LLM: weather now and
      today, calendar, to-dos, unread and important email, health against
      your baseline, the car, parcels, cheap power, Siris'
      overdue tasks/deadlines/inbox/links, and attention counts. Each
      section fails alone: not configured -> `null`; configured but failed
      -> `null` and named in `unavailable`.
    - `GET /siris/hub/v1/widgets` -- car, parcels, energy, protocols and the
      last run (with undo), cameras, the autonomy timeline.
    - `GET /siris/hub/v1/search?q=` -- second-brain notes, past chats and
      memories in one list, each with a `ref` to deep-link with.
    - Protocols (`GET .../protocols/{name}` shows what would run now; `POST
      .../run` and `.../protocols/undo` need `{"confirmed": true}`), `POST
      .../cameras/{camera}/look` (the vision model describes a frame) and
      `POST .../llm/complete` (one plain completion through the model
      router, audited under the caller's task name -- so SirisOS needn't
      keep its own model connection).
    - **The contract is checked on both sides.** Every response is built as
      a pydantic model that forbids unknown fields
      (`app/hub/contract.py`); `contracts/hub-v1/schema.json` is generated
      from them (`python -m app.hub.contract`, a test fails when it drifts);
      `contracts/hub-v1/examples/` holds one real payload per endpoint,
      produced by `tests/test_hub_examples.py` from a faked SirisAI, which
      fails when an example goes stale. SirisOS's tests parse the same
      files. Adding an optional field stays v1; removing or retyping one is
      a v2.
    - **Guest mode** leaves the personal sections (calendar, to-dos, email,
      health, car, parcels, brain) out of the brief and widgets -- they're
      never even read -- and search returns nothing, so a wall tablet
      showing SirisOS is safe with visitors over.
    - Requests carry `X-Siris-User` like the rest of the API, so per-user
      health, memories and chats follow whoever is signed in to SirisOS;
      `X-Siris-Client` names the app in audit triggers ("... in SirisOS").

126. **Done** -- The attention feed: one inbox for everything that needs you
    (`app/hub/attention.py`; `GET /siris/hub/v1/attention`, live at
    `.../attention/stream` as Server-Sent Events, `POST
    .../attention/{id}/act` and `.../dismiss`).
    - **Raised as it happens:** every push notification becomes an alert,
      whoever sent it -- self-healing failures and intruders are urgent, and
      an intruder alert offers "Lock down" (which runs the lockdown
      protocol) next to "It's fine". The same alert again updates the open
      item instead of adding another. An unattended action that needs
      confirmation (a routine, a chat nobody is watching) becomes an
      approval: **Approve** runs the held call with `confirmed=True`,
      **Decline** drops it. This works even without ntfy.
    - **Synced from what SirisAI already keeps** (on request, and every
      `SIRISAI_ATTENTION_SYNC_MINUTES`, default 5): open habit suggestions
      (Create automation / No thanks), jobs waiting for cheap power
      (Cancel), notices queued for the briefing, and second-brain links and
      inbox notes to review. What you dismissed stays dismissed; items close
      themselves when the underlying thing goes away (accepted in chat, run,
      delivered in the briefing).
    - Acting does the real thing under a trigger naming the feed and the
      client ("attention feed: the user chose 'approve' on ... in SirisOS"),
      so the audit log and autonomy timeline show where the decision came
      from. An item can be acted on once; a second tap is a 404, not a
      second run. Dismissals are server-side, so phone and desktop agree.
    - The stream sends a snapshot first, then each change from whichever
      thread made it (the scheduler, a chat, another device), with a
      keepalive every 15 s. `?max_events=N` ends it early for a peek.

127. **Done** -- SirisAI can use what SirisOS owns. SirisAI is the only
    assistant, but the engineering library lives in SirisOS, so until now
    you couldn't ask Siris about it. With `SIRISAI_SIRISOS_URL` and a
    service key shared with SirisOS (`SIRISAI_SIRISOS_SERVICE_KEY` here,
    `SIRISOS_SERVICE_KEY` there), six READ tools
    (`app/plugins/builtin/sirisos.py`, client in `app/sirisos/client.py`):
    - `engineering_standards_search` -- pages from the private standards
      library with citations ("what does AS3500 say about...").
    - `sirishydro_ask` -- SirisHydro's cited answer from that library, or
      its plain "the library doesn't establish this". SirisHydro's
      synthesis itself now runs on SirisAI's model router through
      `/siris/hub/v1/llm/complete` (SirisOS side), so there's one model
      connection, audited, instead of two.
    - `engineering_calculators` / `engineering_calculate` -- the
      deterministic civil/water calculators (Manning, Rational Method,
      Hazen-Williams, Darcy-Weisbach, weirs, orifices, buoyancy...) run by
      SirisOS from the same code its Calculators screen uses, so a spoken
      answer matches the screen and the model never does hydraulics
      arithmetic itself.
    - `engineering_projects` -- the project list (hidden in guest mode:
      client names).
    - `career_cpd_status` / `career_chartership_status` -- Brad's CPD over
      the rolling 3 years against the 150 hours and its minimums (from his
      Engineers Australia record, as imported into SirisOS's Career tab),
      and where he is on the path to Chartered, the NER and Victorian
      registration: next steps, competency elements still needing
      evidence, goals (SirisOS ADR 111; hidden in guest mode).
    - `sirisos_apps_status` -- the state of every app SirisOS monitors.
      SirisAI no longer needs to check those apps again: `integration_health`
      includes them, the morning briefing says "Apps needing a look: APD PM
      is down (Timed out)", and SirisOS itself is an integration target.
    Standards text is untrusted content (the taint guard applies), selector
    hints route engineering prompts to these tools, and six tool-choice
    eval cases cover them. SirisOS accepts the service key only on these
    read/compute routes, never for changing its data.

128. **Done** -- SirisAI and SirisOS, verified together. The SirisOS side
    (its ADR 110) builds on milestones 125-127: the daily brief and search
    each come from one `/siris/hub/v1` call (falling back to the old calls
    on a 404); an inbox merges the attention feed with SirisOS's own items
    (apps down, overdue tasks, the Archive queue), live; Home runs
    protocols with preview, confirm and undo, shows cameras with "What's
    there?", the car, power and parcels; the calculators run server-side
    with 50 parity cases shared by its TypeScript and Python suites; and
    SirisHydro's synthesis comes through `/siris/hub/v1/llm/complete`.
    - **Guest mode now covers the HUD too:** `/siris/hud/summary` leaves the
      calendar, parcels, car and what the brain learned out (unread) while
      it's on, since SirisOS's Home and the wall-tablet HUD both show it.
    - **Live check** (both apps on uvicorn, fakes for Ollama, Home
      Assistant, Frigate, ntfy and Open-Meteo, a real PDF standard
      indexed by SirisOS):
      - A chat about a 450 mm pipe ran `engineering_calculate`, and SirisOS
        answered 0.202 m³/s with the service key.
      - "What does AS3500 say..." ran `sirishydro_ask`. SirisOS found the
        page and asked SirisAI's `/llm/complete` (audited as "hub: sirishydro
        asked for a completion"), and the cited answer came back.
      - The service key got a 403 when it tried to create a project.
      - A routine's held `undo_last_protocol` arrived on SirisOS's inbox
        stream within a second. Approving it, from the API and from the PWA
        in Chromium, undid the lockdown in Home Assistant, under the
        trigger "attention feed: the user chose 'approve' ... in SirisOS".
      - Lockdown from SirisOS was refused without confirmation (409), then
        locked the door and closed the garage.
      - Camera frames and descriptions came through. Guest mode hid the
        personal widgets, HUD tiles and brief sections.
      - The PWA had no horizontal scroll at phone width and no page errors.
        The browser run caught one bug: a sheet opened from a glass card
        rendered inside the card. SirisOS sheets now render in a portal.

129. **Done** -- Siris checks on itself, answers questions, and
    takes Claude chats (vault release 1.2).
    - **Is it still learning?** The nightly upkeep always ends with a quiet
      one-line summary: how many notes were learned or updated in the last
      day, and what the dream found. If nothing was learned for 3 days, or
      the recall score dropped by more than 5 points, it sends a
      high-priority alert instead.
    - **Recall check:** each night runs the vault's `brain.py eval --record`.
      That asks 30 questions with known answer notes, scores how often search
      finds them, and adds the score to the summary and the mind map.
    - **Ask the brain:** `POST /siris/brain/ask {"question": ...}` returns a
      short answer from the notes, naming them, with "as of" dates (see
      "Ask my brain" below). Chat context now shows each note's "as of"
      date too.
    - **Claude chats:** `POST /siris/brain/capture` with `"kind": "chat"`
      saves a whole conversation to the inbox. The upkeep's model pulls out
      the facts and decisions worth keeping, which go through the inbox sort,
      and keeps the chat in `03-Resources/Claude Chats/`. With no model, the
      chats wait and are never filed raw.
    - **Check these notes:** each week, a push lists the 3 most-connected
      notes Siris wrote that Brad hasn't checked. "The Plex note looks
      right" in chat runs the new `brain_mark_reviewed` tool (also `GET
      /siris/brain/review` and `POST /siris/brain/reviewed`). A correction
      goes through `brain_learn`.
    - Gap questions skip missing attachments, which aren't questions.
    - Verified end to end against a copy of the real vault: a saved chat was
      distilled into *Local AI Hardware* and kept in Claude Chats, recall
      scored 23/30 (keyword search only, with no Ollama), and the summary
      went out at low priority.

130. **Done** -- Calendar by date range for SirisOS. `GET
    /siris/hub/v1/calendar?start=YYYY-MM-DD&end=YYYY-MM-DD` returns every
    event from every connected calendar (CalDAV and .ics feeds, with the
    excluded ones left out) between two local dates, up to 62 days, in the
    hub contract's `CalendarEvent` shape. Guest mode hides them, and
    `unavailable` says when there's no calendar or every calendar failed.
    SirisOS's week view and its "log this as CPD" suggestions read it.
    `GET /siris/hub/v1/forecast?days=7` gives the week's daily forecast
    (today first) for the same view. Verified with a faked calendar set in
    `tests/test_hub.py` (local midnight bounds in Australia/Melbourne,
    guest mode, a failing calendar, bad ranges), the forecast tool, and the
    new `calendar.json` and `forecast.json` contract examples.

131. **Done** -- Photos in quick capture. `POST /siris/brain/capture` takes
    an `image` (a JPEG, PNG, HEIC, WebP or GIF data URL, up to 10 MB) with
    or without text. The photo is written to the vault's `Attachments/`
    folder (never overwriting another), embedded in the capture as
    `![[Attachments/...]]`, tagged `photo`, and committed with the note. A
    photo on its own is titled by when it was taken. SirisOS's capture
    button sends photos straight from the iPhone camera. Verified in
    `tests/test_second_brain.py` against a real vault folder.

132. **Done** -- The learning loop: SirisAI learns from how things went.
    Until now it was about as smart on day 300 as on day 1. Now it keeps
    **outcomes** (`app/learning/`, an `outcomes` table) for what happened
    after it acted or suggested something, and changes what it does
    because of them.
    - **Outcomes.** It records:
      - your answer to anything in the attention feed (habit suggestions
        however they're answered, approvals, memory proposals);
      - suggestions left open for 3+ days (*ignored*);
      - device changes and protocols it did on its own that you reversed
        within 15 minutes, or an hour for a protocol (*undone*), or left
        alone (*kept*);
      - chat turns you corrected ("no, I meant...") (*corrected*);
      - calls whose own `verify()` said they didn't work (*unverified*).
      Every outcome names the record it came from, so nothing is counted
      twice.
    - **Asks before repeating what you undo.** An action it takes unasked
      that you've undone or declined 3+ times, more often than kept, is held
      for confirmation next time: an Approve/Decline in the feed with "you
      undid or turned down this 3 of the last 3 times" as the reason, in
      the audit `policy`. Approvals count back in its favour, so it earns
      its way back. Self-healing (always pre-confirmed) and anything you
      ask for in chat are never held.
    - **Quieter suggestions.** Once you've turned down 5+ habit suggestions
      with under 20% accepted, new ones wait in the feed instead of buzzing
      your phone.
    - **Nightly reflection** at `SIRISAI_REFLECTION_HOUR_LOCAL` (3am):
      - catches up on outcomes;
      - turns each correction followed by a different tool into a
        tool-choice **eval case**. Learned cases join
        `evals/tool_choice.jsonl` in every eval run, and
        `GET /siris/learning/eval-cases?format=jsonl` exports them to
        commit;
      - with a model configured, proposes up to three lasting facts from
        the day's corrections and failures as "Remember this?" items with
        Remember / No thanks. Nothing is remembered without a yes, and
        facts already in memory aren't proposed again;
      - posts a short "What I learned last night" note when there's
        something to say.
    - **Says when a change didn't take.** If a tool's own check afterwards
      fails in chat (a device that's offline), the reply ends "Heads up: I
      checked afterwards and light.porch is still 'off'" instead of
      letting "Done" stand.
    - **Weekly goal review** (`SIRISAI_GOAL_REVIEW_DAY`/`_HOUR_LOCAL`,
      Monday 8am) puts each active goal in the feed: "Get chartered: 3 of
      7 done", the next step, and when it last moved. "Done that" ticks
      the step off and "Not this week" puts it away. A goal with no steps
      is asked to be broken down.
    - **Once-a-day jobs** (`app/core/daily_jobs.py`): these and the upkeep
      jobs to come run at their local hour, once per day, surviving
      restarts. `GET /siris/learning/jobs` shows each one's last run and
      result, and `POST /siris/learning/jobs/{name}/run` runs one now.
    - Read it all at `GET /siris/learning` (approval per tool and source,
      what's held for confirmation), `/outcomes`, `/reflections`, or ask
      "what have you learned?" (`what_have_you_learned`).
    - **Verified:** 24 tests in `tests/test_learning.py`. Also live on
      uvicorn with a stand-in Ollama:
      - Asked "remind me about the bins", the model ran
        `get_current_time`. "No, I meant set a reminder for 2 hours" then
        ran `set_reminder`.
      - `POST /siris/learning/reflect` found the correction and learned
        the eval case `{"prompt": "remind me about the bins", "expect":
        ["set_reminder"]}`. The selection eval picked it up (37 cases, up
        from 36).
      - It proposed "When Brad says remind me about something, he wants a
        reminder set". Choosing Remember on SirisOS's
        `/hub/v1/attention/{id}/act` stored it.
      - The goal review raised "Get chartered (CPEng): 0 of 1 done" with
        Done that / Not this week.

133. **Done** -- Upkeep: SirisAI looks after itself (`app/upkeep/`), so it can
    run for months without anyone tending it. Each job runs on the
    once-a-day runner from milestone 132 and reports in the attention feed.
    - **Backups you can trust.** Every night at 2am SQLite's online backup
      API takes a consistent copy while SirisAI runs, gzipped into
      `backups/` next to the database, with the newest 14 kept.
      `SIRISAI_BACKUP_COPY_TO` copies each one to a second folder (a NAS
      mount). On the 1st of the month a **restore drill** unpacks the
      newest backup into a scratch file, runs `PRAGMA integrity_check`,
      compares its tables with the live database, and reads it through
      SirisAI's own stores. A pass is a quiet note; a failure is urgent,
      pushed, and proposed as a GitHub issue.
    - **Housekeeping** (5am):
      - prunes the audit log after 180 days, metrics after 90, closed feed
        items after 60, finished reminders after 90 and outcomes after a
        year, then VACUUMs after a big prune. Conversations and memories
        are never pruned;
      - records database size (including the WAL), free disk space and the
        day's LLM tokens;
      - warns when disk is under `SIRISAI_DISK_WARN_PERCENT` (urgent and
        pushed under half of it), when tokens pass
        `SIRISAI_TOKEN_BUDGET_DAILY`, when the database doubles in a week,
        or when backups go stale;
      - the deploy stack now caps container logs at 3 × 10 MB per app.
    - **Self-update with rollback.** SirisAI checks GitHub releases daily
      at 9am. `SIRISAI_UPDATE_MODE=notify` (the default) offers "SirisAI
      1.4 is out" with the notes and Update now; `auto` updates in that
      hour; `off` doesn't check. Updating writes `update/request.json`,
      carrying today's selection-eval accuracy as the bar to clear.
      `deploy/updater.sh` (cron every minute, `make auto-update`):
      - refuses if either checkout has local edits;
      - fast-forwards SirisAI to the tag, pulls SirisOS, rebuilds;
      - checks both apps answer, SirisAI reports the new version, and the
        selection eval is no worse;
      - otherwise resets both checkouts, rebuilds and writes why to
        `status.json`.
      Back up, SirisAI reports "Updated to 1.4", or pushes "Update to 1.4
      rolled back" with the log tail and proposes an issue. A request
      nobody picks up in 15 minutes means the updater isn't installed, and
      it says so.
    - **Files its own issues.** With `SIRISAI_GITHUB_ISSUES_REPO` and
      `SIRISAI_GITHUB_TOKEN` set, a tool the circuit breaker pauses, a
      rolled-back update or a failed drill becomes "File a GitHub issue?"
      in the feed (`SIRISAI_GITHUB_ISSUES_MODE=auto` files straight away).
      The issue carries the version and the last day's failures, labelled
      `filed-by-sirisai`. Secret-looking argument names and token-shaped
      values are blanked. A repeat comments on the open issue, at most
      once a day.
    - **Model upgrades gated by evals.** `POST /siris/upkeep/models/trial`
      runs the tool-choice evals (including the learned cases) against the
      current model and a candidate on the same Ollama. Each week the same
      happens for every model in `SIRISAI_CANDIDATE_MODELS` not tried in
      four weeks. A candidate scoring at least as well is offered: "Switch
      to qwen3:14b? 92% vs 88%". Switching takes effect at once and
      survives restarts; `/models/switch` with no model reverts. A worse
      one is reported and left alone.
    - `GET /siris/upkeep` shows it all; `POST /backup`, `/restore-drill`,
      `/housekeeping`, `/update/check` and `/update/apply` run things now.
      Ask "are your backups OK?" (`upkeep_status`).
    - **Verified:** 20 tests in `tests/test_upkeep.py`, plus 8 in
      `tests/test_updater.py` that run `updater.sh` against stand-in git,
      docker and curl: applied; refused with local edits; no tag; and
      rolled back after a failed build, an unhealthy start or a worse eval.
      Also live on uvicorn with a stand-in GitHub:
      - a backup copied to the "NAS" folder;
      - the restore drill passed (29 tables);
      - "SirisAI 9.9 is out" offered; Update now wrote `request.json`
        with a 100% baseline;
      - a simulated rolled-back `status.json` was reported within seconds
        with its log tail, and the proposed issue, once filed, reached
        GitHub with the token and label.
      The live run caught two bugs, both fixed: a rollback showed twice in
      the feed, and the database size left out the WAL.

134. **Done** -- More useful day to day: engineering write-ups, CPD that
    logs itself, approvals from the phone, and a sorted inbox before the
    briefing.
    - **Calc notes in one request** (`engineering_calc_note`). "Size the
      outlet pipe for Werribee and write it up":
      - runs the SirisOS calculator;
      - asks SirisHydro the standards question, with numbered citations;
      - matches the SirisOS project by name or id;
      - writes a markdown note (inputs with labels and units, results,
        the cited answer, or "the library doesn't hold enough" when it
        doesn't, plus your notes) and saves it to Siris, tagged
        `calc-note` and linked to the project.
    - **CPD and chartership evidence capture**
      (`app/career/capture.py`, 6pm):
      - calendar events that look like professional development
        (webinars, courses, conferences, training, site inspections,
        IPWEA, Engineers Australia...) become "Log as CPD? IPWEA
        stormwater webinar (1.5 h)", with hours from the event and a
        category guessed from the title (risk management, business and
        management, or area of practice);
      - a day of engineering work with SirisAI becomes "Chartership
        evidence from today?", summarising the calculations and standards
        questions. It suggests the open competency elements SirisOS lists
        that fit;
      - `career_log_cpd` logs one in chat, and `career_drafts` lists them.
      Kept drafts go to Siris and export as CSV
      (`GET /siris/career/drafts?format=csv`) to copy into Engineers
      Australia, where CPD is actually logged.
    - **Approve or Decline from the phone.** A held action's push now
      carries Approve and Decline, posting to its attention item's own
      `/siris/hub/v1/attention/{id}/act`. So a tap does exactly what the
      feed does: runs the call (or doesn't), closes the item everywhere,
      is audited as "in your phone", and counts for the learning loop.
      Previously it carried a single Confirm that ran the tool directly
      and left the item open.
    - **Morning triage** (`app/triage/morning.py`, 6am):
      - for the emails from the last day that need you (up to 5), it
        drafts a reply, saves it in your Drafts folder, and raises "Reply
        to Sam Lee: Werribee RFI" with the draft. **Send** asks first,
        then sends exactly that. Nothing is sent otherwise, and the model
        is told an email's text is data, not instructions;
      - today's events and any clashes go in one "This morning" item.
    - **Verified:** 9 tests in `tests/test_useful.py`. Also live on
      uvicorn with SirisOS and ntfy stood in over real HTTP:
      - `engineering_calc_note` returned the note with "Werribee
        Drainage (active)", 0.198 m³/s and the AS/NZS 3500.2 citation;
      - `career_log_cpd` then `?format=csv` gave the row ready for
        Engineers Australia, and the evening capture offered the day's
        calc as evidence;
      - a routine's held `undo_last_protocol` reached ntfy with Approve
        and Decline. Posting Decline as ntfy would closed the item as
        declined, and the learning loop recorded it against
        `undo_last_protocol`.

135. **Done** -- Marking things done. `POST /siris/brain/done` with
    `{note, task}` ticks that open task in the note the way Obsidian Tasks
    does (`- [x] … ✅ 2026-10-17`), matched as insights shows it; with just
    `{note}` it marks a project finished (`status: done`), which takes its
    deadline and leftover tasks out of insights. Chat gets the same as
    `brain_mark_done` ("I did my run", "the marathon's finished"). Needs
    the Siris vault engine 1.3 (`brain.py done`). SirisOS 2.7 puts a Done
    button on each item in Siris' "Coming up". Verified in
    `tests/test_second_brain.py`.
136. **Done** -- OneNote into Siris (`app/onenote/`). Every night at
    `SIRISAI_ONENOTE_HOUR_LOCAL` (1:00, before the 2:00 upkeep) SirisAI
    reads new and changed pages from Brad's OneNote notebooks through
    Microsoft Graph, read-only. OneNote notebooks in OneDrive aren't files
    you can read off disk, so the API is the only reliable way in.
    - Typed text becomes Markdown: headings, lists, to-dos as `- [ ]`,
      tables and links. Pictures are saved in
      `03-Resources/Attachments/OneNote` and embedded.
    - Handwriting (OneNote ink) is drawn into images, kept with the note,
      and transcribed by the local vision model (`SIRISAI_VISION_MODEL`;
      `qwen2.5vl:7b` reads handwriting best of the small ones). It's marked
      as a machine transcription to check, with `[illegible]` where it
      couldn't read a word.
    - A new page goes to Siris' inbox, so the upkeep's sort files it as a
      new note or merges it into the note on the same topic. A page edited
      later adds only what changed, as a dated update on wherever the page
      ended up.
    - At most `SIRISAI_ONENOTE_MAX_PAGES` (30) pages a night, oldest first,
      so a big first import spreads over a few nights.
      `SIRISAI_ONENOTE_NOTEBOOKS` limits it to some notebooks, and
      `SIRISAI_ONENOTE_SKIP_NOTEBOOKS` leaves some out (`*` wildcards, e.g.
      `ENC*` for old uni course notebooks; their sections aren't even listed).
    - `SIRISAI_ONENOTE_SKIP_IMAGE_PAGES=true` skips pages with pictures or PDF
      printouts, with or without handwriting on them; handwriting on a blank
      page is still imported. A skipped page is read again only if it's edited.
    - `SIRISAI_ONENOTE_SAVE_IMAGES=false` keeps images out of the vault (and so
      out of git): handwriting is transcribed and proofread from the rendered
      images in memory, and the note points to the page in OneNote instead.
    - Microsoft allows about 400 OneNote requests an hour, and a big account
      can't list all its pages in one call, so each section's page list is
      kept and re-listed only when the section changes (plus a few a night as
      a safety net), with at most 100 listings and 120 page reads a night.
    - If the sign-in expires you get one high-priority push saying how to
      sign in again.

    **Setting it up (once):**

    1. At [entra.microsoft.com](https://entra.microsoft.com) (or the Azure
       portal), go to **App registrations** and choose **New registration**.
       Name it "SirisAI". Under supported account types choose **Personal
       Microsoft accounts only**. Leave the redirect URI empty.
    2. In the new app, open **Authentication** and turn on **Allow public
       client flows**. Save.
    3. Under **API permissions**, add **Microsoft Graph**, then
       **Delegated permissions**: `Notes.Read` and `offline_access`.
    4. Copy the **Application (client) ID** into
       `SIRISAI_ONENOTE_CLIENT_ID`, set `SIRISAI_VISION_MODEL=qwen2.5vl:7b`
       (`ollama pull qwen2.5vl:7b` first) and restart SirisAI.
    5. Sign in: `docker exec -it sirisai python -m app.onenote.login` with
       the `deploy/` stack (`sirisai-sirisai-1` if SirisAI runs on its own).
       It prints a code; enter it at microsoft.com/devicelogin and sign in
       with the OneDrive account (brad6491@gmail.com). The token is kept in
       `data/onenote/token.json`, readable by the owner only.

    Verified in `tests/test_onenote.py`.

137. **Done** -- One look across SirisAI and SirisOS. The web UI, HUD and mind
    map now use SirisOS's Liquid Glass design system (its ADR 106) instead
    of SirisAI's old flat grey theme.
    - `app/web/static/siris-glass.css` (served at `/siris-glass.css`)
      carries SirisOS's tokens with the same names and values: near-black
      with ice-blue light, `--brain` violet, the glass fills, rims and
      sheen, and the radii and springs. It also has the static wallpaper
      (colour pools and the faint HUD grid) and the glass material. Change
      a token there when SirisOS changes it.
    - The chat UI has:
      - a floating glass header, with SirisOS's dock "lens" under the
        active tab;
      - a glass pane for every view, with glass cards for memories,
        goals, routines, protocols, cron, gaps and activity;
      - SirisOS's fields and its tinted pill for primary actions;
      - chat bubbles in the accent tint (yours) and glass (Siris's);
      - a pill composer that glows when focused, and thinking shown in
        `--brain` violet.
      Status colours come from `--good`, `--warning` and `--critical`
      instead of hard-coded hex.
    - **Light theme** is opt-in, as in SirisOS: a "Light theme" switch in
      the settings row sets `data-theme="light"`. It's applied before first
      paint, so dark never flashes.
    - The HUD keeps its layout, with SirisOS glass panels and chips over the
      same wallpaper. The mind map's loading screen matches.
    - Lists use the glass look without a backdrop blur (`.glass-card`), so
      long chats and lists scroll smoothly. Everything falls back to solid
      surfaces under `prefers-reduced-transparency` and stops moving under
      `prefers-reduced-motion`.
    - **Verified:** a test checks the stylesheet carries SirisOS's tokens
      and every page loads it. Also screenshotted in Chromium at 1280 px and
      390 px, dark and light: chat, memory, goals and the HUD, with no page
      errors and no horizontal scroll. That caught the API key row wrapping
      on a phone, now fixed.

138. **Done** -- SSH to the servers on the home network (`app/ssh/`). Name
    them in `SIRISAI_SSH_HOSTS` ("nas=brad@192.168.0.127,pi=root@...";
    blank uses the cron hosts) and ask "how's the NAS?" or "restart nginx on
    the web box". It uses the same ambient SSH identity as the cron overview
    (deploy/: `data/sirisai/ssh`), with BatchMode, so a server without its
    key fails fast instead of asking for a password.
    - `ssh_list_servers`, and `ssh_server_health`: one fixed command per
      server for load, CPUs, uptime, memory, disks and failed systemd units,
      with plain warnings ("/mnt/media is 97% full (120G free)", "plex.service
      has failed").
    - `ssh_run` runs a command on a named server and returns stdout, stderr
      and the exit code. Output is capped, keeping the end, and there's a
      timeout. Only configured servers can be reached.
    - **What asks first is decided in code, not by the model.** A command
      runs straight away only if all of these hold:
      - its program is on a short look-only list (uptime, df, free, ls,
        tail, journalctl, `systemctl status`, `docker ps`/`logs`, `ip addr`,
        `zpool status`...);
      - it has no shell operators (`;`, `|`, `&&`, `>`, `$(`, backticks);
      - it doesn't follow output (`-f`) or ping without a count;
      - it doesn't mention anything secret-looking (`.ssh`, keys, `.env`,
        shadow, tokens, `/proc/*/environ`, shell history);
      - it isn't one of the ways a "look" command changes or leaks things
        (`hostname NAME`, `date -s`, `mount DEV DIR`, `ip link set`,
        `docker inspect` and `systemctl show`, which print env vars, or
        `ps e`).
      Anything else is held for Allow/Cancel (or Approve in the attention
      feed when nobody's watching), showing the server and exact command.
      `SIRISAI_SSH_ALLOW_CHANGES=false` makes it look-only;
      `SIRISAI_SSH_SAFE_COMMANDS` adds your own look-only prefixes.
    - Server output counts as untrusted text for the injection guard, so a
      log line can't instruct SirisAI to save or run anything.
    - **Verified:** 59 tests in `tests/test_ssh.py` against a stand-in `ssh`,
      including 32 commands that must not count as look-only. Also live on
      uvicorn against a real OpenSSH server (key-only login, a test user):
      - `df -h /` and the health snapshot came straight back;
      - `touch /tmp/...`, `cat .env` and `uptime; rm -rf ~` were all held
        for confirmation, and nothing ran until confirmed;
      - once confirmed, the touch ran (exit 0);
      - an unreachable server reported "Connection refused", and an unknown
        name was refused.
      The tests caught `ip link set` slipping through as look-only, and a
      review caught `docker inspect`, `systemctl show`, `ps e`, `hostname`,
      `date -s` and `mount` too, all fixed.

139. **Done** -- Reboot and shut down servers (`ssh_power`). "Reboot the NAS",
    "shut down all the servers", "restart the Pi in 10 minutes" and "cancel
    the shutdown" work for one server, a comma-separated list, or `all`,
    over the SSH from milestone 138.
    - **Always asks first, with a tap.** It's CONFIRM_REQUIRED with no
      exceptions. An unattended caller (a routine, a protocol) gets it held
      for Approve in the attention feed, like any other confirmable call.
      It's never approved by a spoken "yes" (`NEVER_BY_VOICE`), because a TV
      or a guest can say yes. The confirmation reads "reboot all", "shutdown
      nas, pi in 10 minutes", and so on.
    - **Safe in what it runs.** For a non-root account it first asks sudo
      about the exact command (`sudo -n -l systemctl reboot`). If the
      account lacks passwordless sudo for it, the reply says exactly that,
      and deploy/README.md has the one-line sudoers rule that allows just
      the power commands. An immediate reboot or shutdown starts two seconds
      later in the background, so the SSH session ends cleanly and is
      reported as started rather than as a dropped connection. A delay uses
      `shutdown -r/-h +N` (up to 240 minutes), and cancel runs
      `shutdown -c`.
    - **Its own server last.** `SIRISAI_SSH_SELF_SERVER` names the machine
      SirisAI runs on, so "all" does that one after the others.
    - **Per-server results.** A server that's unreachable or lacks sudo
      fails on its own line without stopping the rest. A server name that
      doesn't exist stops the whole call before anything runs.
    - `SIRISAI_SSH_ALLOW_POWER=false` turns it off, as does
      `SIRISAI_SSH_ALLOW_CHANGES=false`.
    - **Verified:** 5 more tests in `tests/test_ssh.py`. Also live on
      uvicorn against a real OpenSSH server, with stand-in `systemctl` and
      `shutdown` recording what they were asked:
      - unconfirmed "reboot all" was held;
      - confirmed, `lab` (a normal user, via a sudoers rule allowing only
        the power commands) and `box` (root, marked as SirisAI's own
        server) each got `systemctl reboot` as root, with `box` last;
      - `plain` (no sudo) reported "needs passwordless sudo", and an
        unreachable server reported "Connection refused";
      - a 10-minute delayed shutdown was scheduled, then cancelled;
      - "lab,prod" ran nothing.


140. **Done** -- The hub for SirisOS's day (2.14). SirisOS 2.9 builds the
    following on these:
    - **Tasks, the evening wrap-up and the adaptive Home.** Vault tasks
      (`GET /siris/brain/tasks`, from `brain.py tasks`), reminders
      (`GET /siris/hub/v1/reminders`, with `POST .../{id}/done`) and
      presence (`GET /presence`, from the last UniFi check).
    - **Meeting prep.** Calendar events now carry `attendees`, and
      `GET /email/with?address=` reads the latest emails from those people.
      It uses IMAP and leaves everything unread.
    - **Email triage.** `POST /attention/{id}/act` accepts an edited `body`
      for a triage reply's Send.
    - **The fitness card and the Status page.** `GET /health/days` gives
      metrics per day; the new `running_km` comes from the Health shortcut.
      `GET /upkeep` reports the newest backup, whether it's stale, and an
      update waiting.

    Everything personal is null in guest mode. Verified in
    `tests/test_hub_day.py`, the contract examples and
    `tests/test_second_brain.py`.

141. **Done** -- A tidier Siris, and more ways in (needs the vault engine
    1.61).
    - **Email into Siris** (`app/gmail/`). Give any Gmail message the label
      **Siris** and it comes into Siris' inbox that night (at
      `SIRISAI_GMAIL_HOUR_LOCAL`, 1:00) for the sort to file: subject as the
      title, who sent it and when, the body as Markdown (quoted replies left
      out), pictures and PDFs saved in `03-Resources/Attachments/Email`. It's
      read-only over IMAP: the label is opened read-only, so nothing in Gmail
      changes. Set up: turn on 2-Step Verification for the Google account,
      make an app password at myaccount.google.com/apppasswords, then set
      `SIRISAI_GMAIL_ADDRESS` and `SIRISAI_GMAIL_APP_PASSWORD` and create
      the label. A refused password sends one high-priority push.
    - **Photos are read.** Before the nightly sort, the vision model
      (`SIRISAI_VISION_MODEL`) reads every photo waiting in the inbox (quick
      capture photos, email attachments): handwriting, whiteboards and
      printed pages are transcribed, anything else gets a one-line
      description, marked as a machine reading to check. Each photo is read
      once.
    - **Real titles for quick captures.** A capture titled with its first
      few words ("I've decided I want to add things to…") or a photo's time
      is filed under a proper name the model gives it; the old title stays
      an alias.
    - **Duplicates.** Insights points out two notes that look like one
      topic. In chat, `brain_merge_notes` folds one into the other (nothing
      reworded; the first is archived) and `brain_notes_distinct` says
      they're different, so they're never suggested again.
    - **Work project pages.** The upkeep refreshes a page per project in
      GVW Projects (`Work/GVW/Projects/`), gathering what the notes say
      about it, its open tasks and the people involved.
    Verified in `tests/test_gmail.py`, `tests/test_second_brain_jobs.py` and
    `tests/test_second_brain.py`.

142. **Done** -- What SirisOS's Projects screen needs (2.25).
    `GET /siris/brain/projects` lists every project note: title, status
    ("active" when the note doesn't say), aliases and path.
    `GET /siris/hub/v1/email/about?q=` returns the latest emails with any of
    those words in the subject. It's read-only, searches the last 90 days,
    quotes the words safely, and returns nothing in guest mode. Verified in
    `tests/test_second_brain.py` and `tests/test_hub_day.py`.

143. **Done** -- A year of health for SirisOS's trends (2.26).
    `GET /siris/hub/v1/health/days` now answers up to 400 days (it was 35),
    so SirisOS 4.1 can chart running, exercise and steps month by month.
    Each metric is still a single query. Verified in `tests/test_hub_day.py`.

144. **Done** -- Looking after the servers (`app/servers/`): wake them,
    watch them, keep them updated, check their disks, and know what
    permissions SirisAI has on each.
    - **Wake-on-LAN** (`ssh_wake`, "wake the NAS"):
      - each health check learns the server's MAC from its default
        interface (via `/proc/net/route`, so no `ip` command needed);
        `SIRISAI_SSH_WAKE_MACS` overrides;
      - the magic packet goes every available way: through a relay server
        over SSH (`SIRISAI_SSH_WAKE_RELAY`, since a container can't
        broadcast onto the LAN), Home Assistant's `wake_on_lan`, and a
        direct broadcast;
      - it then polls SSH in the background and posts "nas is awake (took
        48s)" or "nas didn't wake up" with what to check;
      - it always asks first (a spoken "yes" will do; reboot and shutdown
        via `ssh_power` must be confirmed in the app);
      - computers that aren't SSH servers can be woken too: name them in
        `SIRISAI_SSH_WAKE_MACS` (`desktop=aa:bb:cc:dd:ee:ff@192.168.0.50`).
        With an address, SirisAI watches for it to answer on any common port
        (SSH, SMB, RDP, VNC, HTTP); without one it just sends the packet.
    - **The server watch** runs every `SIRISAI_SERVER_WATCH_MINUTES` (15). It
      syncs the attention feed with what's wrong right now, and items close
      themselves when fixed:
      - disks at 90%+ (urgent at 97%), and memory at 95%+;
      - failed systemd services, with **Restart**;
      - a server missing two checks, with **Wake** when its MAC is known.
    - **OS updates** (daily, 7am): apt and dnf. Raises "2 updates waiting on
      nas (1 security)" with **Install updates**, which runs in the
      background and logs; the next check reports a failure. "nas needs a
      reboot" comes with **Reboot**. `ssh_install_updates` does the same in
      chat and asks first.
    - **Disk health** (daily, 6am): SMART health and counters in plain
      words ("sda: SMART health FAILED!, 12 reallocated sectors"), NVMe
      wear, degraded ZFS pools and md RAID, one item per disk.
    - **Permissions** (`ssh_server_permissions`): asks sudo about each exact
      command SirisAI would run and gives the sudoers line for what's
      missing. A failed Restart names the exact rule for that service.
    - **sudo-rs** (Ubuntu 25.10's sudo), tested against sudo-rs and classic
      sudo:
      - `sudo -n -l <command>` behaves the same in both;
      - sudo-rs refuses wildcards in rule arguments, even when running the
        command, so every recommended rule names an exact command (the apt
        options escaped);
      - exact rules work identically on both.
    - `/siris/servers` lists each server with its MAC and last check, and
      `POST /siris/servers/watch`, `/updates` and `/disks` run a check now.
    - **Verified:** 15 tests in `tests/test_servers.py` against a
      programmable stand-in `ssh`. Also live on uvicorn against a real
      OpenSSH server, with sudo-rs as the server's sudo and stand-ins for
      systemctl, apt, apt-get and smartctl:
      - the watch raised the full disk, the failed nginx, and "ghost isn't
        answering" with Wake after two misses;
      - it learned both servers' MACs;
      - updates showed "2 waiting (1 security)" and "needs a reboot";
      - disks showed a failing drive as urgent;
      - Restart, Install updates and Reboot from the feed ran as root
        through sudo-rs on the non-root server;
      - waking `lab` reported "awake", and waking `ghost` posted "didn't
        wake up" after the timeout;
      - a UDP listener caught valid magic packets, sent both through the
        relay and directly.
      The live run caught three bugs, all fixed:
      - two problems on one disk collapsed into the milder one;
      - the MAC lookup needed `ip`;
      - wildcard sudoers rules don't work on sudo-rs.

145. **Done** -- Safer approvals and a Settings page (`app/core/approvals.py`,
    `app/core/settings_overrides.py`).
    - **An approval PIN** (`SIRISAI_APPROVAL_PIN`). With one set, the
      high-risk actions need it wherever they're approved: chat's Confirm,
      the attention feed (SirisOS, the web UI), `POST /siris/tools/{name}/run`,
      the feed's Reboot, Install updates and Restart buttons, and saving
      settings. High-risk means `ssh_power`, `ssh_install_updates`, `ssh_run`
      when it would change something, and anything in
      `SIRISAI_APPROVAL_PIN_TOOLS`.
      - The HTTP layer checks the PIN and marks the request verified;
        `SirisAI.run_tool()` refuses a confirmed high-risk call that wasn't,
        so there's no side door.
      - Without the PIN the reply is 403 `pin_required: ...`. The web UI
        asks for it and sends again.
      - Five wrong PINs in a row lock these for 10 minutes and raise an
        urgent alert. Every wrong PIN is in the audit log.
      - Phone buttons can't carry a PIN, so these items get Decline only.
        A spoken "yes" never approves them.
    - **Approvals expire** (`SIRISAI_APPROVAL_EXPIRY_MINUTES`, 15): an action
      held in the feed for approval can't be approved after that. The item
      closes as expired (410 to the client), and the attention sync closes
      stale ones. Synced items like "nas needs a reboot" are re-checked on
      their own schedule, so they don't expire.
    - **Settings from the web UI** (Settings tab, `GET`/`PUT /siris/settings`):
      servers, approvals and daily-job hours. Saved values go to
      `SIRISAI_OVERRIDES_FILE` (`./data/settings-overrides.json`), win over
      `.env` and survive restarts. They apply at once, except the few read
      at start-up, which the page marks. Each value is validated by the
      same `Settings` model. Secrets and the PIN aren't editable there.
      Saving needs the PIN when one is set, is refused in guest mode, and is
      audited.
    - **Verified:** `tests/test_approvals.py` covers run_tool's guard, the
      lockout, the feed with and without the PIN, expiry, phone buttons,
      voice, and the routes (403, then 200 with the PIN, and 410 for an
      expired approval). `tests/test_settings_ui.py` covers listing without
      secrets, live apply and persistence, validation, clearing an override,
      the PIN and guest mode.

146. **Done** -- Home controls for SirisOS (2.56). `GET /siris/hub/v1/controls`
    lists lights, garage doors (covers whose device class or name says
    garage or gate) and locks, plus Siris and Home Assistant scenes. Set
    `SIRISAI_HUB_CONTROLS` to choose which entities appear and in what order;
    by default every light, garage door and lock is shown, 16 at most.
    `POST /siris/hub/v1/controls/{entity}` performs one of that control's
    own actions, and only on an entity the list would show. Opening or
    closing a garage door, or unlocking, returns 409 until the request says
    `confirmed`; locking never asks. `POST /siris/hub/v1/scenes/{id}/run`
    runs a scene. Guest mode leaves garage doors and locks out entirely.
    Every action goes through `home_assistant_call_service`, so it's
    audited with who tapped what. If `SIRISAI_APPROVAL_PIN_TOOLS` names
    `home_assistant_call_service`, controls refuse with 403 and the PIN is
    entered in SirisAI. Verified in `tests/test_hub_controls.py`.

147. **Done** -- A tidier brain (2.66, with SirisOS 4.4 and vault 1.63).
    - **Calendar events never become notes.** The meeting-notes job,
      `SIRISAI_BRAIN_MEETING_NOTES_HOUR_LOCAL` and
      `SIRISAI_BRAIN_MEETING_CALENDARS` are gone; leftover settings are
      ignored.
    - **Memories from SirisOS:** `POST /siris/brain/capture` with
      `kind: "memory"` puts the text in the memory inbox instead of making a
      note. The nightly upkeep files it onto the note it's about, e.g.
      "Planned for Sunday: an 8 km run" lands on Running. SirisOS's wrap-up
      uses this for the line about the day and tomorrow's top 3, so there
      are no day notes. Verified in `tests/test_second_brain.py` and
      `tests/test_second_brain_jobs.py`.

148. **Done** -- The network, live, for SirisOS's house (2.76).
    `GET /siris/hub/v1/network` returns the UniFi gateway, switches, access
    points and clients as a graph: each node has its uplink (the switch or
    AP it connects through; null means the gateway) and the bytes per
    second it's receiving and sending now, plus the WAN rates and ISP. It
    reads the classic `stat/health`, `stat/device` and `stat/sta` endpoints
    with the same local API key, because the Integration API's lists carry
    no rates. Guest mode leaves clients out. SirisOS 4.5 animates these as
    links between the devices in its 3D model of the house. Verified in
    `tests/test_unifi.py` against recorded shapes. Not yet live-tested
    against the Cloud Gateway Ultra.

149. **Done** -- Network alerts, power use and Apple Health on its own (3.11, for SirisOS 5.3 to 5.5).
    `POST /siris/hub/v1/network/{mac}/block` blocks a client, or lets it
    back in, through the classic `cmd/stamgr` command (it works for a device
    that isn't connected), and is audited. `GET /siris/hub/v1/energy/usage`
    lists every Home Assistant power sensor (W now; kW converted) and
    energy meter (kWh total; Wh converted), plus the whole house's figures
    from `SIRISAI_ENERGY_GRID_ENTITY_ID` and `SIRISAI_ENERGY_METER_ENTITY_ID`
    when they're set. `POST /siris/health/metrics` now also takes Health
    Auto Export's JSON, any number of days at once: steps, active energy
    and exercise are summed per day, heart rates averaged, weight is the
    last reading, sleep comes from `sleep_analysis`, and running km from
    the run workouts. Verified in `tests/test_unifi.py`,
    `tests/test_hub_energy_usage.py` and `tests/test_health_auto_export.py`.
150. **Done** -- OneNote, tidied (3.21, with vault engine 1.86).
    - New OneNote pages get titles that make sense outside their notebook:
      generic ones ("Session 1", "Initial Notes", "Speaker 2: Tina Porou")
      get their section first, and dates like 17/3/22 become 2022-03-17.
      Pages from work notebooks are filed under `Work/` by the vault's
      `tools/work_notebooks.json`.
    - A page whose note Brad deleted is never imported again, even when
      it's edited in OneNote (the vault engine refuses it; the import
      records it as skipped).
    - **Action items.** Before the nightly sort, the general model reads
      emails, OneNote pages and photo transcriptions written in the last
      30 days and adds what Brad has to do as `- [ ] … 📅 date` tasks, so
      they show in insights and SirisOS's task list.
    - **CPEng evidence.** After the sort, a few work notes a night (the
      backlog first) are checked against Engineers Australia's 16
      chartered competency elements; ones that show them are listed on
      *CPEng Chartership* under "Evidence from your notes", with the
      elements and why.
    - The weekly "check these notes" push starts with a handwritten page,
      to compare the transcription with its images.
    Verified in `tests/test_second_brain_jobs.py`, `tests/test_onenote.py`
    and `tests/test_second_brain.py`.

151. **Done** -- Homelab upkeep (`app/homelab/`): container updates, server
    backups, new devices, internet outages, and expiring certificates and
    domains.
    - **Container updates** (`docker_check_updates`, `docker_update_containers`,
      daily at 5am):
      - **Checking:** for each running container, the daemon asks the
        registry for the image's current digest, which is compared with the
        digests the local image was pulled as.
      - **Skipped:** digest-pinned images, locally built images, containers
        labelled `siris.update=false` (or Watchtower's opt-out), containers
        in `SIRISAI_CONTAINER_UPDATE_EXCLUDE`, and SirisAI itself.
      - **Feed:** "2 container updates: plex, sonarr" with **Update all**.
      - **Updating** works as Watchtower does:
        - pull the new image;
        - stop the old container and rename it aside;
        - create the new one from the old one's settings, minus what came
          from the old image (its env, command and labels), so the new
          image's defaults apply;
        - connect the extra networks and start it.
      - **Roll-back:** if the new container stops or its health check fails
        within `SIRISAI_CONTAINER_UPDATE_HEALTH_SECONDS` (90), it's removed
        and the old one is renamed back and started. The feed then says
        "plex's update was rolled back".
    - **Server backups** (`SIRISAI_SERVER_BACKUPS`, `server:zfs:dataset` or
      `server:path:/folder`, daily at 8am):
      - each backup's newest snapshot or file is checked against
        `SIRISAI_SERVER_BACKUP_MAX_AGE_HOURS` (26), urgently at twice that;
      - an empty backup or one that can't be checked is raised too;
      - on the 1st of each month a random file is read back out of the
        newest snapshot or the folder (archives are listed or gzip-tested),
        posting "Backup test passed" or an urgent "failed". This proves the
        backup can be read back, not that every file is intact.
    - **New network devices** (every 5 minutes, from UniFi's `stat/sta`):
      - the first check records what's there without alerting;
      - after that, "New device: Espressif on IoT" shows the hostname,
        maker, network or Wi-Fi, IP and MAC;
      - **Name it** remembers the device (under the name typed in, sent as
        the act `body`), and `network_name_device` does it from chat;
      - **Block** blocks its MAC (milestone 149's `set_blocked`, so it works
        even once the device has left) and needs the approval PIN;
      - the Home Assistant based `new_device_alert` stands down while this
        runs, so a device doesn't alert twice.
    - **Internet outages:**
      - **Probing:** a TCP probe every minute to Cloudflare, Google and
        Quad9, in its own loop so long jobs don't leave gaps. The internet
        counts as down when none of them answer.
      - **While down:** after two failed probes, "The internet is down
        (since 14:02)" appears in the feed, closing with "Back after 25
        minutes".
      - **Speed:** a speed test every 6 hours; a dip is under half the
        30-day median.
      - **Weekly**, on Mondays: "Internet last week: 3 drops, 41 minutes in
        total, the worst Tue 02:00 for 25 minutes. Speed averaged 480/38
        Mbps with 2 dips." `internet_outages` reports any period.
      - Time SirisAI was stopped isn't counted as an outage.
    - **Certificates and domains** (`expiry_check`, daily at 9am):
      - certificates on `SIRISAI_SECURITY_CERT_HOSTS` and on port 443 of
        each SSH server (skipped quietly where there's no HTTPS);
      - domain registrations in `SIRISAI_SECURITY_DOMAINS`, from RDAP;
      - the feed gets anything expiring within 14 days, urgent inside a
        week, and the items close when renewed.
    - **Safety:** container updates and blocking a device need the approval
      PIN when one is set. All the new settings are on the Settings page.
    - **Verified:** 15 tests in `tests/test_homelab.py`, against a stand-in
      Docker Engine, SSH client and UniFi.
      - **Live, against a real Docker daemon** with a local registry, two
        containers whose image tags were moved to new builds:
        - `demo` was recreated on v2 and came up healthy, keeping its env,
          both networks, volume, restart policy and compose label, with the
          new image's defaults;
        - `other`, whose new image never passed its health check, was rolled
          back and is still running v1.
      - **Backups:** the commands ran in a real shell, listing a tarball,
        reading a file, and catching a corrupt archive.
      - **Not tested live:** RDAP (blocked in the test sandbox) and UniFi's
        `stat/sta` (it uses the same endpoint as milestone 148).

152. **Done** -- Home and safety: a weekly energy report, away mode, a camera
    digest, a weekly audit digest, and settings history with undo.
    - **Energy report** (`energy_report`, Mondays at 8am, `app/household/energy.py`):
      - **Sources:** Home Assistant's energy meters (kWh counters that only
        count up, with resets handled) and power sensors, fetched in one
        history request.
      - **Total:** `SIRISAI_ENERGY_METER_ENTITY_ID` (milestone 149's house
        meter) when set, otherwise the devices added up. Solar, export and
        return meters are left out.
      - **Cost:** from the price sensor at the time each kWh was used, or
        `SIRISAI_ENERGY_FLAT_RATE`.
      - **Compared** with the week before, with the biggest users.
      - **Left on overnight:** power sensors averaging over 25 W between
        midnight and 5am on at least 3 of the last 7 nights, minus
        `SIRISAI_ENERGY_IGNORE`.
    - **Away mode** (`SIRISAI_AWAY_MODE`: ask, auto or off; `app/household/away.py`):
      - **Leaving:** when presence sees everyone leave, the feed and the phone
        get "Everyone's out. Run away?", listing the steps, with **Run it**.
        "auto" runs it and says so.
      - **Returning:** the protocol's undo is applied, except for unlocking,
        opening covers or disarming. A phone joining the Wi-Fi isn't proof
        enough to open the house. An unanswered offer closes itself.
      - **Scope:** only a run made by away mode is undone, never one made
        since by something else.
    - **Camera digest** (`SIRISAI_CAMERA_DIGEST_HOURS`, "12,18,22";
      `camera_summary` in chat; `app/household/cameras.py`):
      - Frigate's events since the last digest, per camera: "Front Door: 2
        people (Brad at 09:30, a courier at 11:04), 1 package. Driveway: 1
        car."
      - Couriers come from Frigate+ labels or sub-labels, and faces from
        sub-labels.
      - Frigate's own descriptions are listed under the summary.
      - It goes to the feed and the phone. When nothing was seen there's no
        digest.
    - **Weekly audit digest** (`audit_digest`, Sundays at 6pm, `app/audit/digest.py`):
      - what SirisAI did on its own (the autonomy timeline's classification),
        what it held for your OK, and what it was refused;
      - wrong approval PINs and lockouts, and settings changes;
      - its own background jobs (away mode, camera digest, internet log) now
        count as autonomous.
    - **Settings history** (`GET /siris/settings/history`,
      `POST /siris/settings/undo/{id}`):
      - every change on the Settings page records each value before and
        after, and whether it had been set there before;
      - **Undo** puts back what a change replaced, going back to `.env` or
        the default when the setting hadn't been set there, rather than
        pinning that value;
      - an undo is itself a change you can undo, and it needs the PIN like
        saving;
      - the page lists the last 15 changes.
    - **Verified:**
      - **Tests:** 9 in `tests/test_household.py` (energy against a stand-in
        Home Assistant, including the real client's history shape; away mode
        offer, run and the secure undo; the camera digest's wording and
        schedule), 2 more in `tests/test_settings_ui.py` and 1 in
        `tests/test_audit_digest.py`.
      - **In Chromium:** saving a setting listed the change, and **Undo** put
        the old value back and listed the undo.
      - **Not tested live:** Home Assistant energy data and Frigate events,
        since neither is reachable from the sandbox.

153. **Done** -- Health in one place, with insights (3.51, for SirisOS 5.8).
    - **One store** (`app/health/service.py`):
      - **What it keeps:** every metric Apple Health gives (about 35:
        sleep stages, vitals, VO₂ max, body composition, water, caffeine),
        the night's sleep times, each workout, and notes logged by voice.
        Per person.
      - **Sources:** each source's reading is kept (`health_readings`), and
        the one to trust goes to `health_samples` by
        `SIRISAI_HEALTH_SOURCE_PRIORITY`. So a Shortcut, Health Auto
        Export and a backfill reporting the same day don't double up.
      - **Workouts:** two sources within five minutes are the same workout.
      - **Before this,** SirisOS kept its own richer copy and SirisAI only
        eight metrics, so chat and voice couldn't see readiness.
    - **One reader** (`app/health/auto_export.py`): Health Auto Export's JSON
      and SirisAI's own shape (SirisOS sends its old store across in it once).
      SirisOS's two copies of this reader go in 5.8.
    - **Scores in SirisAI** (`app/health/scores.py`, moved from SirisOS):
      - readiness, the sleep score, training load and vitals against your
        own baseline;
      - a weight trend (exponentially smoothed, kg a week, weeks to a goal);
      - "getting sick": two of temperature, resting heart rate and HRV off
        their usual, two days running.
      - All of it is at `GET /siris/hub/v1/health/overview`, and workouts
        at `GET /siris/hub/v1/health/workouts?days=` (both additive).
    - **Backfill** (`POST /siris/health/apple-export`): the Health app's
      `export.zip`, read as a stream. For counts, only the device with the
      most each day is used, so phone plus watch steps don't double.
    - **Import health:** "Health data hasn't arrived for 2 days" in the feed
      when nothing new has come in for 36 hours. It clears itself.
    - **Insights** (`app/health/insights.py`):
      - **Getting sick** (10am): a warning with this evening's plans and
        **Block my evening** (a Rest event).
      - **What sleep goes with** (weekly): late meetings, late workouts,
        caffeine, alcohol, a warm bedroom, late TV. Nights with and without
        each are compared, with at least 4 each way and 20 minutes to count.
      - **Bedtime:** from your need, this week's debt and tomorrow's first
        event, with a wind-down nudge 45 minutes before and a new
        `wind_down` protocol (dims the lights that are on). Protocol
        selectors can now filter by state.
      - **Goals** tied to a number ("run 1,000 km this year"): progress and
        pace weekly.
      - **The vault's weekly Health note** now has readiness, sleep,
        workouts, weight, signs of illness, these insights, goals and
        logged symptoms.
    - **Logging by voice and photo:**
      - `health_log`: water, caffeine, drinks, mood and energy, symptoms,
        supplements, medications;
      - `health_log_meal`, and `POST /siris/health/meal` with a photo for the
        local vision model's rough estimate, always labelled an estimate.
    - **Privacy** (`SIRISAI_HEALTH_PRIVATE`, on):
      - **Chat answers:** health tool results never go to a cloud model. A
        local Ollama model writes the answer, or SirisAI's own summary is
        the answer.
      - **History:** the conversation keeps a placeholder, so later cloud
        turns don't carry the numbers.
      - **Planner:** in planner mode a cloud model isn't offered the health
        tools.
      - **Guest mode** hides all `health_*` tools and endpoints.
    - **Export and delete:**
      - `GET /siris/health/export.csv`;
      - `GET /siris/health/report`, a printable summary for a doctor (print
        to PDF);
      - `DELETE /siris/health/data?start=&end=&source=`, which rebuilds the
        trusted values from what's left.
    - **Verified:**
      - **New tests:** 13 in `tests/test_health_store.py`:
        - source priority and falling back after a delete;
        - workout de-duplication;
        - the native import feeding the overview;
        - the weight trend and the illness rule;
        - an `export.zip` (phone and watch steps not doubled, sleep stages,
          a workout) through the upload route;
        - export, report, delete and guest mode;
        - quick logs, meals and goals;
        - the stale alert;
        - the sick warning blocking the evening;
        - sleep insights, bedtime and wind-down once an evening;
        - a cloud model never being sent health data or seeing it in
          history.
      - **Updated tests:** the Health Auto Export test, the vault note test
        and the hub examples.
154. **Done** -- Quieter device alerts and container updates in the background (3.52).
    A device only counts as new when UniFi itself first saw it within 24
    hours and nobody has named it in UniFi. A device that is just
    reconnecting is recorded quietly, and an alert already open for it is
    closed. "Update all" in the feed now starts the update in the
    background and answers at once. The proxy containers (caddy, traefik,
    nginx, https) update last, so the phone isn't cut off mid-request, and
    a notice saying how many updated follows. Verified in
    `tests/test_homelab.py`.

155. **Done** -- Readiness from the night, and an energy score for the day (3.62, for SirisOS 5.9).
    Readiness moved during the day for two reasons. The day's HRV was an
    average of every reading, so a workout or a stressful afternoon pulled
    it down. And each new Health Auto Export rewrote the day, including
    "last night's" sleep, with an afternoon nap folded in or only the tail
    of the night. Now:
    - HRV measured while asleep is kept as `hrv_overnight`, for the
      wake-up day, and readiness uses it.
    - A night on file is only replaced by a fuller view of it.
    - Once the night is in and it's past
      SIRISAI_HEALTH_READINESS_LOCK_HOUR (10am; 2pm at the latest), the
      score is kept as it was.

    The new energy score (`energy` in the health overview,
    `app/health/battery.py`) is a Body Battery. It starts from readiness at
    waking, drains with time awake, active energy and stress (heart rate
    well over resting while still), and gets a little back from rest and
    mindful minutes. It uses hourly data when the export is aggregated by
    hour; otherwise it's estimated from the day's totals and workouts.
    Verified in `tests/test_health_energy.py`.

156. **Done** -- Container updates you can follow, and a watch afterwards (3.72).
    While "Update all" runs, one feed item says where it's got to
    ("Updating 7 of 22: sonarr"), live in SirisOS's Inbox, and closes when
    the run finishes. A container that fails its update or is rolled back
    has its last log lines in the alert. For
    SIRISAI_CONTAINER_UPDATE_WATCH_MINUTES (10; 0 turns it off) afterwards,
    each updated container is checked every 30 seconds. One that stops,
    keeps restarting or turns unhealthy is put back on its previous image
    with the same settings and networks, audited and alerted. Verified in
    `tests/test_homelab.py` (a crash after the update rolls back; a healthy
    one is left alone; progress shown and closed).
    Any feed action that takes longer than 20 seconds (a reboot, or an update
    on an older SirisAI that ran it in the request) now answers then with
    "still working", carries on, and raises a notice ("Done: ...") or an
    alert when it ends. A phone gives up on a request after about a minute
    ("Load failed"). Verified in `tests/test_attention.py`.

157. **Done** -- Health trends and an early heads-up (3.82, for SirisOS 6.1).
    The health overview has `trends`: this week's average against the 30
    days before it, for overnight HRV (or the day's HRV without it),
    resting heart rate, sleep, respiratory rate, steps, exercise and VO2
    max. Each gives the change, up/down/steady (under 3% is steady) and
    whether that's good for you. The getting-sick check now has a level.
    Two of the three signs (temperature, resting heart rate, HRV) today
    alone is `early`: an info notice and a push, a day before the
    two-days-running `likely` warning, which still blocks the evening and
    rests the training plan. Verified in `tests/test_health_store.py`.

158. **Done** -- Network nodes say what UniFi knows (3.83, for SirisOS 6.2).
    Each client in `/siris/hub/v1/network` now carries `named` (a name is
    set in UniFi) and `first_seen` (when UniFi first saw it, epoch seconds).
    The network as a whole says `device_alerts` when SirisAI raises its own
    new-device alerts. With these, SirisOS treats a device UniFi already
    knows as known, and leaves new devices to SirisAI rather than alerting
    twice. Verified by the hub contract examples.

159. **Done** -- Ask about your own past (3.93).
    `health_history` answers questions about any stretch of days: "how did
    I sleep the week before the deadline", "was my HRV down in September".
    For each metric it gives the average, lowest, highest, days with data,
    and the change against the 30 days before. It also returns the morning
    readiness scores kept for those days and the workouts. Chat finds the
    dates first (calendar, notes) when a question names an event. Like the
    other health tools it's private: never sent to a cloud model. SirisOS
    6.3 adds home jobs and appliances to search, so "heat pump" says when
    it was last serviced. Verified in `tests/test_health_energy.py`.

160. **Done** -- Strain, a weekly sleep report, a Health Auto Export check, the brief spoken, and an updater you can see (4.03, for SirisOS 6.4).
    - **Strain** (`app/health/strain.py`, `strain` in the health
      overview): 0 to 21 from heart rate against your maximum, in five
      weighted zones (an hour at 70-80% is about 14), per workout and for
      the day. The day also counts the hours' heart rate from Health Auto
      Export at a quarter weight. Today's target comes from readiness:
      Primed 14-18, Steady 10-14, Recover 4-9. Max heart rate is
      SIRISAI_HEALTH_MAX_HR, else the highest a workout reached in six
      months.
    - **The week's sleep** (`sleep_week`): average against need, debt,
      bedtime regularity, the best and worst nights, what went with
      shorter sleep, and one thing to try.
    - **Setup check** (`setup`): what the last exports looked like. It
      flags data grouped by day, a partial day (the wrong date range), and
      no workouts automation, each with the setting to change.
    - **Spoken brief** (`app/voice/morning.py`): the first time
      SIRISAI_SPOKEN_BRIEF_TRIGGER turns on within
      SIRISAI_SPOKEN_BRIEF_HOURS (5-11), the weather, first events,
      readiness, strain target and inbox are spoken on
      SIRISAI_SPOKEN_BRIEF_PLAYER through SIRISAI_SPOKEN_BRIEF_TTS (HA's
      tts.speak). Once a day, never in guest mode.
    - **The updater checks in**: `deploy/updater.sh` writes a heartbeat
      each minute. The upkeep status says whether it's running, what's
      waiting and how the last update went, so SirisOS can offer an
      Update button.
    - **`scripts/check.sh` and a pre-push hook** run what CI runs: clear
      bytecode caches, check the contract schema, and run the suite on
      Python 3.12.

    Verified in `tests/test_health_energy.py`, `tests/test_spoken_brief.py`
    and `tests/test_updater.py`.

161. **Done** -- Health Auto Export today and sleep fix (4.04): "Previous 7 Days" sends the seven *full* days before today, so today's sleep, steps, strain and energy never arrived. The setup check now flags an export sent today that stops at yesterday and says to use Date Range **Default** (all of yesterday plus today so far). Sleep sent grouped by hour (split at midnight) or as one segment per stage (Core/Deep/REM/Awake) is joined back into one night on the wake-up day; a nap doesn't replace the night. Bedtime no longer counts a trip that started today and runs over tomorrow morning as tomorrow's first event (it said "bed by 07:28" at breakfast). Verified with tests for both sleep shapes and the new check.

162. **Done** -- SI units everywhere (4.05): active energy in spoken and chat health summaries, goal titles and meal notes is given in kJ (stored in kcal as Apple Health sends it; `service.kj`), and a car range reported in miles is converted to km. Verified with tests for the kJ wording and the miles-to-km range.

163. **Done** -- SirisGYM in strain and readiness history (4.06): SirisOS 6.8 sends each day's gym sessions (source `sirisgym`, SirisAI's own shape). Strain counts one at a resistance-training weight (1.5 per minute) unless it overlaps a workout from another source (the Watch), which says it better (`strain.without_doubles`). The health overview adds `readiness_recent`, the last five mornings' kept readiness, which SirisGYM uses to suggest a deload. Verified with tests for the overlap, the load and the shape SirisOS sends.

164. **Done** -- Ask about your training (4.16): SirisOS 6.9 posts SirisGYM's summary after each workout (`POST /siris/health/gym`: recent workouts, each main lift's estimated 1RM over its last sessions, this week by muscle, records in 30 days, deload state, the next routine day with weights). The `gym_progress` tool answers "how's my bench trending", "what should I do in the gym today", "am I training legs enough" from it (`app/health/gym.py`); it's a private health tool, kept off cloud models with SIRISAI_HEALTH_PRIVATE, and the tool selector picks it for gym words. The weekly Health note gets a gym line. Verified with tests for the endpoint, the tool's answers, privacy and selection.

165. **Done** -- Health data you can trust (4.17). Split syncs: summed metrics are kept hour by hour (`service.merge_hour_sums`), so Health Auto Export's Batch Requests add up and re-sends replace their hour. Alerts: `check_stale` goes by Health Auto Export's own last import, defaults to 6 hours (was 36), speaks only 10am-10pm, and adds "Today's health data isn't arriving" when exports stop at yesterday. `POST /siris/health/metrics?dry_run=1` parses without keeping (SirisOS's Test the sync). `GET /siris/health/sources?day=` shows each source's value per metric and which counts. New private tool `health_explain` (why readiness was low: the weakest contributors and the evening before). A monthly training and health report goes to the Health note on the 1st (`insights.monthly`). Verified with tests for each.

166. **Done** -- SirisGym (4.18): the name everywhere is now SirisGym (it was SirisGYM). SirisOS 6.94 puts routine days on set weekdays; its summary says when today's a rest day and which weekday the next workout is on, and gym_progress says so ("Today's a rest day. Next on Thursday: …"). Verified with a test for the rest-day answer.
167. **Done** -- Set-by-set effort (4.28): Health Auto Export workout uploads now keep each Watch workout's minute-by-minute heart rate and active energy. SirisOS 6.96 sends each SirisGym session with the time every set was ticked (`POST /siris/health/gym/session`); SirisAI links it to the Watch workout that overlaps it most, lines heart rate up with each set, and works out per-exercise effort (peak heart rate against max, blended with RPE), recovery between sets and estimated 1RM (`GET /siris/health/gym/session/{id}/analysis`). gym_progress mentions the latest session's effort. Verified with a test that links a session to a Watch workout and checks the per-set numbers.
168. **Done** -- Finer gym effort (4.39): heart rate sent reading by reading from Health Auto Export's Health Metrics automation (not aggregated) is kept for ten days and used instead of the Watch workout's minute-by-minute heart rate when it's finer, and even with no Watch workout. Each set now has a settle time (how long heart rate took to come halfway back down), which SirisOS 7.1 uses to learn the rest timer, and each set and exercise carries its SirisGym id. The training summary mentions cardio minutes, sore spots and heart rate at the same weight. Verified with tests for 5-second readings (a 50 s settle) and the summary text.
169. **Done** -- Editing and presence (4.49): `POST /siris/brain/edit` replaces a note's text through the brain engine's new `edit` command (engine 1.98), refused if the note changed since its `version` was read; `PUT /siris/memory/{id}` corrects a memory's words (same id, date and recall count, new embedding). Presence counts phones matched ignoring case and punctuation, by UniFi name or hostname: SIRISAI_PRESENCE_DEVICE_NAMES plus phones added from SirisOS (`PUT /siris/hub/v1/presence/phones`, kept in state), with Dom's iPhone added by default, so everyone has left only when all of them have gone. `GET /siris/hub/v1/presence` lists each phone and whether it was home at the last check. Verified with tests for the edit route, memory correction, and presence staying home until both phones leave.

## Connecting your calendars (Gmail and iCloud)

SirisAI merges every calendar you connect into one. That one calendar
answers "what's on today" in chat, fills the HUD's next events and the
briefings, and feeds SirisOS's daily brief and home screen.

**iCloud** connects over CalDAV:

1. At [account.apple.com](https://account.apple.com), go to **Sign-In and
   Security**, then **App-Specific Passwords**, and create one called
   "SirisAI".
2. Set `SIRISAI_CALDAV_URL=https://caldav.icloud.com`.
3. Set `SIRISAI_CALDAV_USERNAME` to your Apple ID email.
4. Set `SIRISAI_CALDAV_PASSWORD` to the app-specific password.

Every iCloud calendar is included. New events created by SirisAI go here.

**Gmail / Google Calendar** connects through each calendar's secret
address. Google's CalDAV needs OAuth, but every Google calendar has a
private, read-only iCal address.

1. In [Google Calendar](https://calendar.google.com), open **Settings**,
   pick the calendar under "Settings for my calendars", then go to
   **Integrate calendar**.
2. Copy **Secret address in iCal format**.
3. Add it as `SIRISAI_CALENDAR_ICS_URLS=Gmail|https://calendar.google.com/calendar/ical/.../basic.ics`.
   For more calendars, add one per line or separate them with commas, each
   as `Label|URL`.

Anyone with that address can read the calendar, so keep it secret. If it
leaks, reset it on the same page. Feeds are cached for 5 minutes.

**What's left out:**

- Calendars named in `SIRISAI_CALENDAR_EXCLUDE`, for example `Birthdays`.
- Home Assistant calendars whose id or name contains
  `SIRISAI_HOME_ASSISTANT_CALENDAR_EXCLUDE` (by default Sonarr, Radarr,
  Lidarr and Readarr). Those are media schedules, not your day. "What's on
  TV this week" still works through the `sonarr_`/`radarr_calendar`
  tools.

The HUD, the briefings (morning, bedtime, departure) and SirisOS all use your
connected calendars, and only fall back to Home Assistant's calendars when
none is connected.

## Running locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env
uvicorn app.main:app --reload
```

- `GET /health` -- liveness + DB connectivity check.
- `GET /siris/status` -- core service status: app name, environment, LLM
  provider, registered tools.
- `POST /siris/chat` -- call the configured LLM backend, with registered
  tools offered and prior turns of `conversation_id` replayed as context.
  `{"prompt": "hello"}` -> `{"conversation_id": "...", "response": "...",
  "tool_call": null}`, or if the model calls a tool, `{"conversation_id":
  "...", "response": null, "tool_call": {"name": ..., "arguments": {...},
  "result": ...}}`. Pass the returned `conversation_id` back on the next
  call to continue that conversation; omit it (or pass an unknown one) to
  start a new one. Pass `"use_tools": false` to disable tool offering, or
  `"use_context": false` to skip the context engine's system message.
  Every response carries `"model_role"` (`"general"` or `"fast"`) saying
  which model router role handled the turn; pass `"model_role": "fast"`
  to force one (falls back to `"general"` if that role isn't configured).
  If the model picks a `CONFIRM_REQUIRED` tool, the response carries
  `"confirmation_required": {"name": ..., "arguments": {...}}` instead of
  `tool_call`, and nothing has run yet. Returns 502 if no backend is
  configured/reachable, or if the chosen tool call fails; 403 if it picked
  a `RESTRICTED` tool that isn't enabled (shouldn't normally happen --
  those aren't offered to the model in the first place).
- `POST /siris/chat/confirm` -- `{"conversation_id": ..., "tool_name": ...,
  "arguments": {...}}`, re-submitting exactly what a `confirmation_required`
  response returned, to actually run that tool call. Same response shape as
  `/siris/chat`'s tool-call case.
- `GET /siris/conversations/{conversation_id}` -- the full stored history
  for a conversation (including tool exchanges), most recent last. 404 if
  the id is unknown.
- `GET /siris/activity?limit=50` -- recent tool executions, most recent
  first: tool name, arguments, result or error, duration, and the
  triggering `conversation_id` when there was one.
- `POST /siris/events` -- push an event in from an external system:
  `{"source": ..., "description": ..., "entity_name": null, "data": null}`.
- `GET /siris/events?entity_name=...&limit=50` -- recent events, most
  recent first, same shape as `recall_events`.
- `POST /siris/tools/{name}/run` -- invoke any registered tool or skill
  directly, bypassing the LLM. `{"arguments": {...}}` ->
  `{"tool_call": {...}}`, or `{"confirmation_required": {...}}` for a
  `CONFIRM_REQUIRED` tool -- pass `"confirmed": true` in a second call
  with the same arguments to actually run it. 404 for an unknown tool
  name, 403 for a blocked `RESTRICTED` tool.
- `POST /siris/proactive/check` -- run `server_health_check` and, if it's
  unhealthy and `SIRISAI_NTFY_TOPIC` is set, send one notification
  summarizing the warnings. `{"result": {...}, "notified": true|false}`.
  Meant to be called on a schedule from outside this process (see
  `.env.example` for a crontab line), not something SirisAI triggers
  itself.
- `POST /siris/memory` -- save a memory directly, e.g. `{"content": "..."}`.
- `GET /siris/memory?query=...&limit=20` -- list/search saved memories,
  most recent first.
- `POST /siris/voice/transcribe` -- multipart file upload (`file`) ->
  `{"text": "..."}`. Off by default (`SIRISAI_STT_PROVIDER=none`); returns
  502 if no backend is configured/reachable.
- `POST /siris/voice/speak` -- `{"text": "..."}` -> `audio/mpeg` bytes. Off
  by default (`SIRISAI_TTS_PROVIDER=none`); returns 502 if no backend is
  configured/reachable.
- `GET /` -- the web UI.

All `/siris/*` routes require `Authorization: Bearer <SIRISAI_API_KEY>` once
that env var is set; `/health` and `/` never require it. For anything
reachable off `localhost` (e.g. tunneled in via Tailscale), set
`SIRISAI_API_KEY` -- generate one with `openssl rand -hex 32`.

## Using SirisAI from a phone

**Add to Home Screen.** Open the web UI in Safari, tap Share, then Add to Home
Screen. It installs as an app (own icon, no browser bar) and reopens your last
conversation. The History menu lists past ones.

**Siri and Apple Shortcuts.** `POST /siris/ask` returns the answer as plain
text, so a Shortcut can speak it:

1. New Shortcut. Add **Ask for Input** (Text, "What do you want to ask?").
2. Add **Get Contents of URL**: URL `http://<server>:8000/siris/ask`, Method
   POST, Headers `Authorization: Bearer <your SIRISAI_API_KEY>`, Request Body
   File with the *Provided Input*.
3. Add **Speak Text** with the *Contents of URL*.
4. Name the Shortcut "Ask Siris", then say "Hey Siri, Ask Siris".

Actions that need confirmation (locks, garage doors, automations, code changes)
are not run from Shortcuts: the reply tells you to approve them in the web UI.

**Ask my brain.** The same Shortcut works with `POST /siris/brain/ask`. That
answers only from Siris, names the notes it used, and says how
current they are. Make the Request Body **JSON** with a `question` field set
to the *Provided Input*, then **Speak Text** with the dictionary value
`answer`.

**Send a Claude chat to Siris.** In the Claude app, copy the conversation
(or share it), then run a Shortcut that sends the text to the brain:

1. New Shortcut that accepts Text from the share sheet (or uses **Get
   Clipboard**).
2. **Get Contents of URL**: `http://<server>:8000/siris/brain/capture`, Method
   POST, the same `Authorization` header, Request Body JSON with `text` set
   to the input, `kind` set to `chat` and, optionally, a `title`.
3. Overnight, the upkeep pulls out what's worth keeping and files it.

**Reminders and timers.** "Remind me at 7pm to take the bins out" and "set a
timer for 10 minutes" arrive as push notifications (ntfy), sent by the
background scheduler.

## Optional: voice (speech-to-text / text-to-speech)

The mic button and the speaker icon on assistant replies only appear once
`SIRISAI_STT_PROVIDER`/`SIRISAI_TTS_PROVIDER` are set to `openai_compatible`
(see `.env.example`) -- without a backend configured they're deliberately
hidden rather than shown broken.

This project's own server runs [speaches](https://speaches.ai)
(`ghcr.io/speaches-ai/speaches:latest-cpu`), a single CPU-only container
providing both OpenAI-compatible endpoints (faster-whisper for STT, Kokoro
for TTS) -- CPU-only so it never competes with Ollama for GPU:

```yaml
# ~/Docker/Speaches/docker-compose.yml
services:
  speaches:
    image: ghcr.io/speaches-ai/speaches:latest-cpu
    restart: unless-stopped
    ports:
      - "8085:8000"
    volumes:
      - hf-hub-cache:/home/ubuntu/.cache/huggingface/hub
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/health"]
volumes:
  hf-hub-cache:
```

Download its models once (inside the running container, since it already
has `uv`):

```bash
docker exec -e SPEACHES_BASE_URL=http://localhost:8000 speaches-speaches-1 \
  uv tool run speaches-cli model download Systran/faster-distil-whisper-small.en
docker exec -e SPEACHES_BASE_URL=http://localhost:8000 speaches-speaches-1 \
  uv tool run speaches-cli model download speaches-ai/Kokoro-82M-v1.0-ONNX
```

Then point SirisAI at it (its own `.env`/override, not speaches'):

```
SIRISAI_STT_PROVIDER=openai_compatible
SIRISAI_STT_BASE_URL=http://<server>:8085
SIRISAI_STT_MODEL=Systran/faster-distil-whisper-small.en
SIRISAI_TTS_PROVIDER=openai_compatible
SIRISAI_TTS_BASE_URL=http://<server>:8085
SIRISAI_TTS_MODEL=speaches-ai/Kokoro-82M-v1.0-ONNX
SIRISAI_TTS_VOICE=af_heart
```

`SIRISAI_TTS_SPEED` sets the speaking pace (sent to the backend only when it
isn't 1.0), and `SIRISAI_TTS_EFFECT` runs every spoken clip through an
ffmpeg `-af` filter chain (`app/voice/effects.py`; ffmpeg ships in the
image) -- see `.env.example` for a light "electronic" chain. The effect is
applied once, around whichever TTS backend is configured, so the chat page,
the HUD and satellites all sound the same; if ffmpeg fails, the unprocessed
audio is used instead.

Two UI toggles next to the API key field control voice/thinking behavior per
browser (persisted in `localStorage`, default off/on respectively): **Auto-play
voice replies** speaks every assistant reply automatically instead of needing
the speaker icon clicked, and **Show thinking live** controls whether the
reasoning panel auto-expands while streaming vs. staying collapsed (still
generated either way, just not shown by default).

### The mic needs a secure context

Browsers only expose microphone recording (`navigator.mediaDevices`) on a
secure context -- `https://` or `http://localhost`, not a plain LAN
`http://<ip>:8000` address. This project's own server front-ends SirisAI with
a self-signed HTTPS reverse proxy for exactly this reason:
`docker compose --profile https up -d` starts a Caddy container
(`caddy/Caddyfile`) on port 8443. Two things matter for its config:

- The site address must name real hostnames it's reachable as (e.g.
  `<lan-ip>:8443, localhost:8443`), not a hostless `:8443` -- Caddy can't
  pre-issue a cert without knowing what name(s) to put in it, and the
  on-demand fallback for a hostless site fails the TLS handshake outright.
- `default_sni <lan-ip>` (a global option) is required for LAN-IP access to
  work at all: RFC 6066 forbids the SNI extension for IP-literal hostnames,
  so curl/browsers connecting to `https://<lan-ip>:8443` send **no SNI**
  (only `openssl s_client -servername <ip>` fakes it, which is why testing
  with that alone looks fine while real clients don't connect).

First visit shows a self-signed-certificate warning (Caddy's root CA isn't in
any device's trust store) -- click through once ("Advanced" -> "Proceed");
browsers remember that per origin.

To make HTTPS invisible:

- **Redirect the pages.** Set `SIRISAI_HTTPS_REDIRECT_URL=https://<lan-ip>:8443`
  and opening `/`, `/hud` or `/brain` over plain http redirects there (query
  string included, so `/hud?key=...` still works). Only those pages move: the
  API, satellites, ntfy action buttons and icons stay on `http://<lan-ip>:8000`.
  Requests arriving through Caddy carry `X-Forwarded-Proto: https`, so there's
  no redirect loop.
- **Trust the root certificate once per device**, and the warning goes away.
  It's served at `https://<lan-ip>:8443/sirisai-root-ca.crt` (click through the
  warning one last time to download it).
  - iPhone/iPad: open that link in Safari -> Allow -> Settings -> "Profile
    Downloaded" -> Install. Then Settings -> General -> About -> Certificate
    Trust Settings -> turn on full trust for "Caddy Local Authority".
  - Mac: open the file -> Keychain Access -> double-click "Caddy Local
    Authority" -> Trust -> "Always Trust".

  A device that trusts this root accepts any certificate signed with its key,
  and that key lives in the `caddy_data` volume on the server. Don't delete that
  volume or `/data/caddy/pki`: Caddy would generate a new root, and every device
  would need to trust it again.

## Optional: Obsidian relay

The Obsidian Local REST API plugin only listens on 127.0.0.1 inside its own
container. `docker compose --profile obsidian-relay up -d` starts a small socat
sidecar that shares that container's network and republishes the port, so it
survives restarts (recreate the sidecar if the Obsidian container is recreated).
Set `SIRISAI_OBSIDIAN_BASE_URL=https://<server>:27123`.

## Running with Docker

**SirisAI and SirisOS together** (recommended for a home server): see
[`deploy/README.md`](deploy/README.md). `cd deploy && ./setup.sh && make up`
runs both, wired to each other, with one `.env` for the stack and one
settings file per app.

SirisAI on its own:

Two ways to get a container running, same `.env` and `./data` bind mount
either way:

**Build from source** (this repo's own `docker-compose.yml` -- what this
project's own server runs, since it's under active development here):

```bash
docker compose up --build
```

**Pull a prebuilt image** (no clone needed, the Radarr/Sonarr-style path --
GitHub Actions builds and pushes `ghcr.io/bradmairs/sirisai` on every push to
`main`, see `.github/workflows/docker-publish.yml`):

```yaml
# docker-compose.yml
services:
  sirisai:
    image: ghcr.io/bradmairs/sirisai:latest
    ports:
      - "8000:8000"
    volumes:
      - ./data:/app/data
    env_file:
      - .env
    environment:
      SIRISAI_DATABASE_URL: sqlite:///./data/sirisai.db
    restart: unless-stopped
```

```bash
docker compose pull && docker compose up -d
```

SQLite data persists to `./data` on the host via a bind mount.

## Versions and releases

SirisAI 1.0 was the first numbered release; `CHANGELOG.md` lists every
version since. Each change merged to `main` bumps the version: +0.01 for a
small one, +0.1 for a significant one (`python scripts/bump_version.py
small|significant "<summary>"`). A new version on `main` is tagged
`v<version>` and released on GitHub automatically, and the Docker image is
published as `ghcr.io/bradmairs/sirisai:<version>` as well as `:latest`.
`GET /siris/status` reports the running version.

## Tests

```bash
pytest
```
