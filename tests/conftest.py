import os
import tempfile
from collections.abc import Iterator

# Must run before `app.main` (and therefore `app.core.config`) is imported --
# points Settings at a `.env` file that doesn't exist, so tests get
# deterministic defaults regardless of a real local `.env` in the repo root.
os.environ.setdefault("SIRISAI_ENV_FILE", ".env.test")

# Same reasoning, for the database: the default DATABASE_URL points at the
# same `./data/sirisai.db` a locally-running dev server uses, so without
# this, running `pytest` writes real test rows into that file -- confirmed
# live (2026-09-03): repeated test_integrations.py runs left 16 fake
# "myapi-<uuid>" integrations registered as real tools on a running local
# dev server, because integrations are loaded from the DB at startup, not
# just from .env. Point tests at an isolated temp file instead.
os.environ.setdefault("SIRISAI_DATABASE_URL", f"sqlite:///{tempfile.mkdtemp()}/test.db")

# Same reasoning again, for automatic memory extraction (a hardening
# pass): it defaults to enabled in real deployments (the whole point of
# it), but that means a real extra LLM call after every chat turn that
# produces a response -- most existing tests construct a fake LLM client
# tailored to exactly one call, or assert an exact call count/order, and
# have no reason to expect a second, unrelated call landing right after
# theirs. Tests for the feature itself opt back in explicitly via their
# own `Settings(auto_memory_extraction_enabled=True, ...)`.
os.environ.setdefault("SIRISAI_AUTO_MEMORY_EXTRACTION_ENABLED", "false")

# The planner is on by default in real deployments (see Settings.default_use_planner).
# Most existing tests fake an LLM tailored to the single-tool flow, so they opt out
# here; tests for the default itself pass use_planner / Settings explicitly.
os.environ.setdefault("SIRISAI_DEFAULT_USE_PLANNER", "false")

# Tool selection narrows what the model is offered; tests fake an LLM that picks
# by name, so they see the full list. Tests for the selector build it directly.
os.environ.setdefault("SIRISAI_TOOL_SELECTION_MAX", "0")

# The background scheduler would fire during the suite; its jobs are tested directly.
os.environ.setdefault("SIRISAI_SCHEDULER_ENABLED", "false")

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture(scope="session")
def client() -> Iterator[TestClient]:
    # Using TestClient as a context manager runs the app's lifespan
    # (Base.metadata.create_all, etc) exactly as a real ASGI server would.
    with TestClient(app) as test_client:
        yield test_client
