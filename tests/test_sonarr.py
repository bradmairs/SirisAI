import json

import httpx

from app.plugins.builtin.sonarr import SonarrListSeriesTool, SonarrQueueTool, SonarrSearchSeriesTool
from app.sonarr.client import SonarrClient


def _mock_client(handler) -> SonarrClient:
    return SonarrClient(base_url="http://sonarr.local:8989", api_key="test-key", transport=httpx.MockTransport(handler))


def test_list_series_maps_fields_and_sends_api_key_header() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v3/series"
        assert request.headers["x-api-key"] == "test-key"
        return httpx.Response(200, json=[{"id": 1, "title": "Breaking Bad", "year": 2008, "monitored": True, "status": "ended"}])

    client = _mock_client(handler)
    result = SonarrListSeriesTool(client).run()
    assert result == [{"id": 1, "title": "Breaking Bad", "year": 2008, "monitored": True, "status": "ended"}]
    client.close()


def test_queue_maps_records() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v3/queue"
        return httpx.Response(
            200,
            json={"records": [{"title": "Breaking Bad S01E01", "status": "downloading", "timeleft": "00:05:00", "size": 500, "sizeleft": 50}]},
        )

    client = _mock_client(handler)
    result = SonarrQueueTool(client).run()
    assert result == [
        {"title": "Breaking Bad S01E01", "status": "downloading", "timeleft": "00:05:00", "size": 500, "sizeleft": 50}
    ]
    client.close()


def test_search_series_posts_series_search_command() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v3/command"
        body = json.loads(request.content)
        assert body == {"name": "SeriesSearch", "seriesId": 7}
        return httpx.Response(200, json={"id": 55, "status": "queued"})

    client = _mock_client(handler)
    result = SonarrSearchSeriesTool(client).run(series_id=7)
    assert result == {"id": 55, "status": "queued"}
    client.close()
