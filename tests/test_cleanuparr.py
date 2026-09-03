"""Cleanuparr client/tool tests -- deliberately only cover the single
status check app/cleanuparr/client.py's docstring flags as this
codebase's least-verified integration; see that docstring for why
nothing else is built (or tested) here.
"""

import httpx

from app.cleanuparr.client import CleanuparrClient
from app.plugins.builtin.cleanuparr import CleanuparrStatusTool


def _mock_client(handler) -> CleanuparrClient:
    return CleanuparrClient(base_url="http://cleanuparr.local:11011", api_key="test-key", transport=httpx.MockTransport(handler))


def test_status_hits_health_path_and_sends_api_key_header_and_parses_json() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/health"
        assert request.headers["x-api-key"] == "test-key"
        return httpx.Response(200, json={"status": "healthy"})

    client = _mock_client(handler)
    result = CleanuparrStatusTool(client).run()
    assert result == {"status": "healthy"}
    client.close()


def test_status_falls_back_to_plain_text_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="OK", headers={"content-type": "text/plain"})

    client = _mock_client(handler)
    result = CleanuparrStatusTool(client).run()
    assert result == {"status": "OK"}
    client.close()
