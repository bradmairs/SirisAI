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
    client.py     # CalDAV client (wraps the `caldav` library)
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
      httpx elsewhere. `calendar_upcoming_events`, `READ`.

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

## Running with Docker

```bash
docker compose up --build
```

SQLite data persists to `./data` on the host via a bind mount.

## Tests

```bash
pytest
```
