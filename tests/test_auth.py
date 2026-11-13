import asyncio

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.core.auth import require_api_key
from app.core.config import Settings, get_settings
from app.main import app


def _request(host: str = "127.0.0.1"):  # type: ignore[no-untyped-def]
    from starlette.requests import Request

    async def receive():  # type: ignore[no-untyped-def]
        return {"type": "http.request", "body": b"", "more_body": False}

    return Request({"type": "http", "method": "GET", "path": "/siris/status", "headers": [], "client": (host, 1), "query_string": b""}, receive)


def test_require_api_key_allows_when_unset() -> None:
    asyncio.run(require_api_key(_request(), authorization=None, settings=Settings(api_key=None)))
    with pytest.raises(HTTPException):  # 6.68: but only from this machine
        asyncio.run(require_api_key(_request("192.168.0.50"), authorization=None, settings=Settings(api_key=None)))


def test_require_api_key_rejects_missing_or_wrong_header() -> None:
    settings = Settings(api_key="secret")

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(require_api_key(_request(), authorization=None, settings=settings))
    assert exc_info.value.status_code == 401

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(require_api_key(_request(), authorization="Bearer wrong", settings=settings))
    assert exc_info.value.status_code == 401


def test_require_api_key_allows_correct_header() -> None:
    asyncio.run(require_api_key(_request(), authorization="Bearer secret", settings=Settings(api_key="secret")))


def test_siris_routes_require_key_when_configured(client: TestClient) -> None:
    app.dependency_overrides[get_settings] = lambda: Settings(api_key="secret-key")
    try:
        assert client.get("/siris/status").status_code == 401
        assert client.get("/siris/status", headers={"Authorization": "Bearer wrong"}).status_code == 401

        response = client.get("/siris/status", headers={"Authorization": "Bearer secret-key"})
        assert response.status_code == 200
    finally:
        app.dependency_overrides.pop(get_settings, None)


def test_health_never_requires_a_key(client: TestClient) -> None:
    app.dependency_overrides[get_settings] = lambda: Settings(api_key="secret-key")
    try:
        assert client.get("/health").status_code == 200
    finally:
        app.dependency_overrides.pop(get_settings, None)
