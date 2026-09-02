import os
from collections.abc import Iterator

# Must run before `app.main` (and therefore `app.core.config`) is imported --
# points Settings at a `.env` file that doesn't exist, so tests get
# deterministic defaults regardless of a real local `.env` in the repo root.
os.environ.setdefault("SIRISAI_ENV_FILE", ".env.test")

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture(scope="session")
def client() -> Iterator[TestClient]:
    # Using TestClient as a context manager runs the app's lifespan
    # (Base.metadata.create_all, etc) exactly as a real ASGI server would.
    with TestClient(app) as test_client:
        yield test_client
