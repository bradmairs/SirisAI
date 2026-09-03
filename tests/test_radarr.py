import json

import httpx

from app.plugins.builtin.radarr import RadarrListMoviesTool, RadarrQueueTool, RadarrSearchMovieTool
from app.radarr.client import RadarrClient


def _mock_client(handler) -> RadarrClient:
    return RadarrClient(base_url="http://radarr.local:7878", api_key="test-key", transport=httpx.MockTransport(handler))


def test_list_movies_maps_fields_and_sends_api_key_header() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v3/movie"
        assert request.headers["x-api-key"] == "test-key"
        return httpx.Response(
            200,
            json=[{"id": 1, "title": "The Matrix", "year": 1999, "monitored": True, "hasFile": True, "status": "released"}],
        )

    client = _mock_client(handler)
    result = RadarrListMoviesTool(client).run()
    assert result == [
        {"id": 1, "title": "The Matrix", "year": 1999, "monitored": True, "has_file": True, "status": "released"}
    ]
    client.close()


def test_queue_maps_records() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v3/queue"
        return httpx.Response(
            200,
            json={"records": [{"title": "The Matrix", "status": "downloading", "timeleft": "00:10:00", "size": 1000, "sizeleft": 200}]},
        )

    client = _mock_client(handler)
    result = RadarrQueueTool(client).run()
    assert result == [
        {"title": "The Matrix", "status": "downloading", "timeleft": "00:10:00", "size": 1000, "sizeleft": 200}
    ]
    client.close()


def test_search_movie_posts_movies_search_command() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v3/command"
        body = json.loads(request.content)
        assert body == {"name": "MoviesSearch", "movieIds": [42]}
        return httpx.Response(200, json={"id": 99, "status": "queued"})

    client = _mock_client(handler)
    result = RadarrSearchMovieTool(client).run(movie_id=42)
    assert result == {"id": 99, "status": "queued"}
    client.close()
