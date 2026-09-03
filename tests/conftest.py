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

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture(scope="session")
def client() -> Iterator[TestClient]:
    # Using TestClient as a context manager runs the app's lifespan
    # (Base.metadata.create_all, etc) exactly as a real ASGI server would.
    with TestClient(app) as test_client:
        yield test_client
