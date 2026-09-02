from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture(scope="session")
def client() -> Iterator[TestClient]:
    # Using TestClient as a context manager runs the app's lifespan
    # (Base.metadata.create_all, etc) exactly as a real ASGI server would.
    with TestClient(app) as test_client:
        yield test_client
