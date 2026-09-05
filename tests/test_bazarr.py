import httpx

from app.bazarr.client import BazarrClient
from app.plugins.base import ToolPermission
from app.plugins.builtin.bazarr import BazarrSearchWantedSubtitlesTool, BazarrWantedEpisodesTool, BazarrWantedMoviesTool


def _mock_client(handler) -> BazarrClient:
    return BazarrClient(base_url="http://bazarr.local:6767", api_key="test-key", transport=httpx.MockTransport(handler))


def test_wanted_movies_maps_rows_and_sends_api_key_header() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/movies/wanted"
        assert request.headers["x-api-key"] == "test-key"
        return httpx.Response(200, json={"data": [{"title": "The Matrix", "missing_subtitles": ["en"]}]})

    client = _mock_client(handler)
    result = BazarrWantedMoviesTool(client).run()
    assert result == [{"title": "The Matrix", "missing_subtitles": ["en"]}]
    client.close()


def test_wanted_episodes_maps_rows() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/episodes/wanted"
        return httpx.Response(
            200,
            json={"data": [{"seriesTitle": "Breaking Bad", "episode_title": "Pilot", "missing_subtitles": ["en"]}]},
        )

    client = _mock_client(handler)
    result = BazarrWantedEpisodesTool(client).run()
    assert result == [{"series_title": "Breaking Bad", "episode_title": "Pilot", "missing_subtitles": ["en"]}]
    client.close()


# -- search_wanted_subtitles / bazarr_search_wanted_subtitles


def test_search_wanted_subtitles_posts_the_task_id() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/system/tasks"
        assert request.url.params["taskid"] == "search_wanted_subtitles"
        return httpx.Response(200, json={})

    client = _mock_client(handler)
    result = BazarrSearchWantedSubtitlesTool(client).run()
    assert result == {"triggered": True}
    client.close()


def test_search_wanted_subtitles_is_a_safe_action() -> None:
    assert BazarrSearchWantedSubtitlesTool.permission is ToolPermission.SAFE_ACTION
