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
  llm/            # Model-agnostic LLM client interface
    base.py       # LLMClient ABC + NullLLMClient placeholder
    ollama.py     # OllamaClient -- the first real backend
    factory.py    # Picks a backend from settings.llm_provider
  plugins/        # Tool/plugin interface + registry
    base.py       # Tool ABC + ToolRegistry
    builtin/      # Reference/first-party tools (echo.py, memory.py)
  memory/
    service.py    # MemoryStore -- SirisAI's long-term memory (SQLite-backed)
  voice/          # Model-agnostic speech interface (STT + TTS)
    base.py       # SpeechToTextClient/TextToSpeechClient ABCs + Null placeholders
    openai_compatible.py  # client for any server speaking OpenAI's audio API shape
    factory.py    # Picks backends from settings.stt_provider/tts_provider
  home_assistant/
    client.py     # Thin sync REST client for Home Assistant's HTTP API
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

The rest of this list is proposed, not built -- captured here so the next
round of work has a concrete order rather than an open-ended brainstorm.
Same rule as everything above: tiny, working-state milestones, each one
extending an existing pattern rather than introducing a parallel one.

10. Conversation persistence -- `/siris/chat` is currently single-turn and
    stateless (each call is independent; only what the model chooses to
    `remember` survives). Store each exchange (user/assistant/tool
    messages) against a conversation id in SQLite so a client can carry
    context across turns without resending history itself. Foundational
    for memory categories (14) and the context engine (16) below.
11. Tool execution audit log -- every tool call (what was asked, tool
    name, arguments, result, duration, timestamp) recorded to a SQLite
    table and exposed via `GET /siris/activity`, surfaced in the web UI as
    an activity feed. Worth building before tools gain any real power --
    makes it possible to see exactly what SirisAI did and why.
12. Tool permission levels -- extend `Tool` with a level (`READ` /
    `SAFE_ACTION` / `CONFIRM_REQUIRED` / `RESTRICTED`). `SirisAI.run_tool()`
    executes `READ`/`SAFE_ACTION` immediately; `CONFIRM_REQUIRED` returns a
    pending action for the caller to confirm before it actually runs;
    `RESTRICTED` requires an explicit opt-in in config. Needed before the
    tool library below gains anything destructive (restarting containers,
    applying updates).
13. Expand the tool library, one tool at a time, each gated by the
    permission levels above: Docker (container status/restart/update),
    system info (disk/CPU/temperature), Plex, UniFi, weather, calendar.
    Same `Tool` pattern as `echo`/`remember`/`recall`/`home_assistant_*` --
    no new mechanism, just more tools.
14. Memory categories -- split the current flat `MemoryStore` into entity
    memory (people/things: "Brad", "Plex", "the NAS") and event memory
    (things that happened: "Plex crashed", "backup failed"), alongside the
    existing free-text long-term memory. Lets SirisAI answer "has Plex
    been playing up lately?" from actual event history instead of just
    current state.
15. Skills -- composable workflows that chain multiple tool calls into one
    reusable unit (e.g. a health-check skill running several system/Docker
    checks and summarizing the result), invocable by the model like a tool
    or triggered directly. Worth more once the tool library (13) actually
    has enough to compose.
16. Context engine -- before calling the LLM, assemble a small curated
    context package (recent events, system health, relevant memories)
    instead of sending just the raw prompt, so an answer to "how's
    everything looking?" has real situational awareness without every
    prompt carrying the whole database.
17. Event bus -- inbound webhook endpoint(s) so external systems (Docker,
    Home Assistant, UniFi, Plex, calendar) can push events into SirisAI
    instead of only being polled through tools. Logged as event memory
    (14); SirisAI decides whether to ignore, log, investigate, or notify.
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
- `POST /siris/chat` -- single-turn call to the configured LLM backend, with
  registered tools offered. `{"prompt": "hello"}` -> `{"response": "...",
  "tool_call": null}`, or if the model calls a tool, `{"response": null,
  "tool_call": {"name": ..., "arguments": {...}, "result": ...}}`. Pass
  `"use_tools": false` to disable tool offering. Returns 502 if no backend
  is configured/reachable, or if the chosen tool call fails.
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
