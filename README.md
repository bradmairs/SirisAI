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
  llm/base.py     # Model-agnostic LLM client interface (no backend yet)
  plugins/        # Tool/plugin interface + registry
    base.py       # Tool ABC + ToolRegistry
    builtin/      # Reference/first-party tools (echo.py proves the pattern)
```

**Extension points, by design:**
- **New tool** -- subclass `Tool` in `app/plugins/builtin/` (or your own
  package), register it in `SirisAI._register_builtin_tools`.
- **New LLM backend** -- implement `LLMClient` in `app/llm/`, wire it into
  `SirisAI` based on `settings.llm_provider`. Nothing outside `app/llm/`
  should know which backend is active.
- **New route** -- add a router under `app/api/routes/`, include it in
  `app/main.py`. Keep routes thin; put logic on `SirisAI` or a tool.

## Milestones

Deliberately tiny, each one a working state:

1. **This scaffold** -- FastAPI app in Docker, SQLite wired up, config +
   logging, `SirisAI` core service, `/health` and `/siris/status`.
2. Add a real LLM call (Ollama-backed `LLMClient`).
3. Wire the tool interface into an actual endpoint (LLM decides to call a
   tool, tool result comes back in the response).
4. Memory subsystem -- SirisAI records/recalls what it learns about you.
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

## Running with Docker

```bash
docker compose up --build
```

SQLite data persists to `./data` on the host via a bind mount.

## Tests

```bash
pytest
```
