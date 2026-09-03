"""Listenarr client/tool tests -- deliberately only cover the two
endpoints app/listenarr/client.py's docstring flags as near-universal
across the Servarr family; see that docstring for why nothing beyond
system/status and queue is built (or tested) here.
"""

import httpx

from app.listenarr.client import ListenarrClient
from app.plugins.builtin.listenarr import ListenarrQueueTool, ListenarrSystemStatusTool


def _mock_client(handler) -> ListenarrClient:
    return ListenarrClient(base_url="http://listenarr.local:8787", api_key="test-key", transport=httpx.MockTransport(handler))


def test_system_status_hits_correct_path_and_sends_api_key_header() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/system/status"
        assert request.headers["x-api-key"] == "test-key"
        return httpx.Response(200, json={"version": "1.0.0"})

    client = _mock_client(handler)
    result = ListenarrSystemStatusTool(client).run()
    assert result == {"version": "1.0.0"}
    client.close()


def test_queue_maps_records() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/queue"
        return httpx.Response(200, json={"records": [{"title": "Some Audiobook", "status": "downloading"}]})

    client = _mock_client(handler)
    result = ListenarrQueueTool(client).run()
    assert result == [{"title": "Some Audiobook", "status": "downloading"}]
    client.close()
