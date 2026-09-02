# SirisAI

A Jarvis-style assistant for the homelab: ask it questions, have it query
and control things on the home network, and eventually let it remember what
it learns about you along the way. This repo is the scaffold -- solid
foundations first, capability added in small, shippable steps.

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
5. Auth.
6. Homelab/home-automation tools (Home Assistant, etc.).

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

## Running with Docker

```bash
docker compose up --build
```

SQLite data persists to `./data` on the host via a bind mount.

## Tests

```bash
pytest
```
