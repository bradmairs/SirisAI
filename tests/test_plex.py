import httpx

from app.plex.client import PlexClient
from app.plugins.builtin.plex import PlexLibrarySectionsTool, PlexNowPlayingTool


def _mock_client(handler) -> PlexClient:
    return PlexClient(base_url="http://plex.local:32400", token="test-token", transport=httpx.MockTransport(handler))


def test_now_playing_maps_sessions() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/status/sessions"
        assert request.headers["x-plex-token"] == "test-token"
        return httpx.Response(
            200,
            json={
                "MediaContainer": {
                    "Metadata": [
                        {
                            "title": "The Matrix",
                            "type": "movie",
                            "duration": 8160000,
                            "viewOffset": 2040000,
                            "User": {"title": "brad"},
                            "Player": {"state": "playing"},
                        }
                    ]
                }
            },
        )

    client = _mock_client(handler)
    result = PlexNowPlayingTool(client).run()
    assert result == [
        {"title": "The Matrix", "type": "movie", "user": "brad", "state": "playing", "progress_percent": 25.0}
    ]
    client.close()


def test_library_sections_maps_directories() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/library/sections"
        return httpx.Response(
            200,
            json={"MediaContainer": {"Directory": [{"key": "1", "title": "Movies", "type": "movie"}]}},
        )

    client = _mock_client(handler)
    result = PlexLibrarySectionsTool(client).run()
    assert result == [{"key": "1", "title": "Movies", "type": "movie"}]
    client.close()
