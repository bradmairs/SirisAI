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
    factory.py    # Picks a backend from settings.llm_provider
  plugins/        # Tool/plugin interface + registry
    base.py       # Tool ABC (+ ToolPermission) + ToolRegistry
    builtin/      # Every tool: echo, memory, home_assistant, system_info,
                  # docker, plex, unifi, weather, calendar
  skills/
    base.py       # Skill ABC + SkillTool (adapts a Skill to the Tool interface)
    builtin/      # server_health.py -- reference skill
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
18. Model router -- support multiple configured LLM backends at once
    (building on the existing `LLMClient` interface) with policy-based
    selection -- a fast local model for simple asks, a larger one for
    general conversation, a coding model, an optional cloud model -- plus
    a "keep private data local" routing policy.
19. Proactive notifications -- anomaly detection over the event/metric
    history from milestones 14 and 17 (e.g. "the backup normally finishes
    by 3am but hasn't today"), surfaced unprompted through a notification
    channel rather than waiting to be asked. The furthest-out item on this
    list -- depends on the event bus and event memory actually
    accumulating real history first, and stops well short of SirisAI
    taking autonomous action on its own; that's deliberately not planned
    yet.
20. Voice satellites -- extend the existing STT/TTS primitives
    (milestones 7-8) beyond the built-in web UI to standalone
    always-listening devices (mic + speaker + wake word) around the
    house, talking to the central SirisAI server.

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
