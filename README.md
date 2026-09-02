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
    `/api/chat`, just always a single-item one. Tool exchanges are
    persisted but not yet replayed to the model as structured context on
    later turns (still just user/assistant text) -- full tool-result
    round-tripping remains future work, same boundary milestone 3 drew.
    The built-in web UI now carries a conversation across turns within a
    page session, with a "New chat" button to reset it.

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
    still not built. What ships instead is the other half of "proactive":
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
    and would have been overclaiming a rigor that isn't really there.
    What it does implement is real, just more modest: every tool result
    is fed back to the model before it decides on a next step, so
    "verification" is the model re-assessing actual result *content*
    against the original request each iteration (not just whether the
    call raised) -- if the evidence doesn't add up, it calls another
    tool; if it does, it answers. A CONFIRM_REQUIRED tool mid-plan still
    stops the loop and surfaces `confirmation_required` with whatever
    steps already ran (see `PlanResult.pending_confirmation`) --
    resuming the rest of the plan after confirming is out of scope here;
    `POST /siris/chat/confirm` runs just that one step, same as it
    already does for a single non-planned tool call. Every step still
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
25. Simulation / dry-run mode. `Tool` gets an optional `dry_run()`
    alongside `run()` -- for a restart/update-shaped tool, reports what
    it *would* do (which containers restart, active Plex sessions that'd
    drop) without doing it. Sequenced before 24 finishes maturing rather
    than after, deliberately: a planner that can chain multiple
    SAFE_ACTION/CONFIRM_REQUIRED tool calls is exactly where a dry run
    matters most, so it should exist by the time the planner is trusted
    with real chains, not bolted on afterward.
26. Notification intelligence. Replaces the flat "unhealthy -> send one
    ntfy message" logic in `POST /siris/proactive/check` (19) with a
    `NotificationManager` that scores each event (severity, confidence,
    category) and decides ignore / log / daily-briefing / notify / urgent,
    plus correlates related events into one message ("Plex is down,
    likely the media disk's I/O latency") instead of three separate ones.
    Depends on 21 (the world model is what makes "Plex down + disk
    latency up" a correlated pair instead of two unrelated facts).
27. Personalized thresholds. `server_health_check` (15) hard-codes
    CPU/memory/disk >90%. This replaces that with learned per-metric
    baselines (mean/spread over recent history) so "67% CPU" can be
    flagged as unusual for *this* server without an arbitrary global
    cutoff. Same shape as milestone 19's honest scope-cut: a fresh install
    has no history to learn a baseline from, so this needs a metrics
    history table accumulating first -- likely ships with a documented
    "needs N days of data before it's meaningfully different from the old
    fixed threshold" caveat rather than pretending day-one baselines mean
    anything.
28. Explainable autonomy. Every autonomous action (anything a
    CONFIRM_REQUIRED/SAFE_ACTION tool does without a human directly typing
    "do X right now") gets its trigger, evidence, and the policy that
    permitted it recorded alongside the existing audit entry (11) --
    enough to actually answer "why did you restart Plex last night" from
    stored data instead of the model guessing after the fact. Extends
    `AuditLogStore` with these fields rather than inventing a parallel
    log.
29. Routine engine. Natural-language routines ("every night, check my
    server and tell me tomorrow if anything went wrong") stored as
    structured data (name, trigger, ordered actions, output), introspectable
    via 22, rather than living only as a prompt. Open design question,
    flagged rather than silently decided: milestone 19 deliberately kept
    SirisAI without an in-process scheduler, so an external cron/systemd
    timer decides *when* things run and SirisAI just decides *what*
    happens when asked. Structured routines make "what is SirisAI
    scheduled to do" answerable, but *running* them on a schedule either
    means teaching the external trigger to enumerate stored routines
    (keeps 19's boundary) or adding a real in-process scheduler (reverses
    it). Worth deciding explicitly when this milestone starts rather than
    drifting into a scheduler by accretion.
30. Reflection / maintenance cycle. A self-inspection skill (same `Skill`
    pattern as 15's `server_health_check`) that reports on SirisAI itself:
    unused/duplicate memories, tools that fail often, integrations
    erroring. The one worth calling out specifically is "frequently
    requested action without a matching skill" -- if the same multi-step
    request keeps coming through the planner (24) instead of a reusable
    skill, that's a concrete, data-backed suggestion ("you've asked this
    5 times, want a skill for it?") instead of a vague one.
31. Capability gap detection. When a request needs something SirisAI has
    no tool for, report *what's missing* instead of just failing --
    ideally naming a plausible data source/integration and a concrete
    tool spec shape (e.g. asked about UPS battery health, no tool exists,
    but Network UPS Tools' API is a known fit for `ups.get_status()`).
    Depends on 22 (capabilities are only knowable to compare against once
    introspectable) and benefits from 23/24 (a goal or plan is what
    surfaces the gap in the first place, not just a bare chat message).
    The output is a spec a human (or Claude Code, working this same repo)
    can act on -- this milestone stops at generating that spec, it
    doesn't attempt to write and load its own tools at runtime.
32. Automatic tool discovery. A generic integration layer that ingests an
    OpenAPI spec (`POST /integrations` with a spec URL) and generates
    `Tool` instances from it, rather than every integration being a
    hand-written `app/<service>/client.py` + `app/plugins/builtin/<service>.py`
    pair like 13's six. Realistic scope cut to flag now, before it starts:
    auto-generated tools should default to READ permission unless a
    human explicitly upgrades one, since a generated tool has no
    hand-reviewed judgment behind it the way 12's permission levels
    assumed every tool would.
33. Cortex. A capstone, not a new capability: once 21/23/24/28 exist, the
    request -> context -> plan -> memory/world-model -> tools -> verify ->
    memory -> response pipeline described in the brainstorm is mostly
    already sitting across `ContextEngine` (16), the planner (24), and
    `SirisAI.run_tool()` (11) -- this milestone is naming and consolidating
    that flow into one orchestration path so `/siris/chat` calls into it
    instead of assembling context, calling the model, and handling tool
    calls inline the way it does today. The point isn't a new subsystem,
    it's making the LLM client visibly swappable infrastructure behind a
    stable orchestration surface, which is really an outcome of 1-32
    being done rather than an independent milestone -- sequenced last on
    purpose.

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
