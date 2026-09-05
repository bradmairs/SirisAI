import httpx

from app.plex.client import PlexClient
from app.plugins.builtin.plex import (
    PlexLibrarySectionsTool,
    PlexNowPlayingTool,
    PlexSearchTool,
)


def _mock_client(handler) -> PlexClient:
    return PlexClient(
        base_url="http://plex.local:32400",
        token="test-token",
        transport=httpx.MockTransport(handler),
    )


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
        {
            "title": "The Matrix",
            "type": "movie",
            "user": "brad",
            "state": "playing",
            "progress_percent": 25.0,
        }
    ]
    client.close()


def test_library_sections_maps_directories() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/library/sections"
        return httpx.Response(
            200,
            json={
                "MediaContainer": {
                    "Directory": [{"key": "1", "title": "Movies", "type": "movie"}]
                }
            },
        )

    client = _mock_client(handler)
    result = PlexLibrarySectionsTool(client).run()
    assert result == [{"key": "1", "title": "Movies", "type": "movie"}]
    client.close()


def test_search_flattens_hubs_into_one_list_with_the_source_library_attached() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/hubs/search"
        assert request.url.params["query"] == "dune"
        return httpx.Response(
            200,
            json={
                "MediaContainer": {
                    "Hub": [
                        {
                            "title": "Movies",
                            "type": "movie",
                            "Metadata": [
                                {
                                    "title": "Dune",
                                    "type": "movie",
                                    "year": 2021,
                                    "summary": "A mythic journey.",
                                }
                            ],
                        },
                        {
                            "title": "Tracks",
                            "type": "track",
                            "Metadata": [
                                {
                                    "title": "Dune (Theme)",
                                    "type": "track",
                                    "year": 2021,
                                    "summary": None,
                                }
                            ],
                        },
                        {
                            "title": "Actors",
                            "type": "actor",
                        },  # a hub with no Metadata key at all
                    ]
                }
            },
        )

    client = _mock_client(handler)
    result = PlexSearchTool(client).run(query="dune")
    assert result == [
        {
            "title": "Dune",
            "type": "movie",
            "year": 2021,
            "library": "Movies",
            "summary": "A mythic journey.",
        },
        {
            "title": "Dune (Theme)",
            "type": "track",
            "year": 2021,
            "library": "Tracks",
            "summary": None,
        },
    ]
    client.close()


def test_search_returns_an_empty_list_for_no_hubs() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"MediaContainer": {}})

    client = _mock_client(handler)
    result = PlexSearchTool(client).run(query="nonexistent")
    assert result == []
    client.close()
