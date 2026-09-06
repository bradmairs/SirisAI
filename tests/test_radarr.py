import json

import httpx
import pytest

from app.plugins.base import ConfirmationRequired, ToolPermission
from app.plugins.builtin.radarr import (
    RadarrAddMovieTool,
    RadarrCalendarTool,
    RadarrDeleteMovieTool,
    RadarrListMoviesTool,
    RadarrListQualityProfilesTool,
    RadarrListRootFoldersTool,
    RadarrLookupMovieTool,
    RadarrQueueTool,
    RadarrRemoveQueueItemTool,
    RadarrSearchMovieTool,
)
from app.radarr.client import RadarrClient


def _mock_client(handler) -> RadarrClient:
    return RadarrClient(
        base_url="http://radarr.local:7878",
        api_key="test-key",
        transport=httpx.MockTransport(handler),
    )


def test_list_movies_maps_fields_and_sends_api_key_header() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v3/movie"
        assert request.headers["x-api-key"] == "test-key"
        return httpx.Response(
            200,
            json=[
                {
                    "id": 1,
                    "title": "The Matrix",
                    "year": 1999,
                    "monitored": True,
                    "hasFile": True,
                    "status": "released",
                }
            ],
        )

    client = _mock_client(handler)
    result = RadarrListMoviesTool(client).run()
    assert result == [
        {
            "id": 1,
            "title": "The Matrix",
            "year": 1999,
            "monitored": True,
            "has_file": True,
            "status": "released",
        }
    ]
    client.close()


def test_queue_maps_records() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v3/queue"
        return httpx.Response(
            200,
            json={
                "records": [
                    {
                        "id": 9,
                        "title": "The Matrix",
                        "status": "downloading",
                        "timeleft": "00:10:00",
                        "size": 1000,
                        "sizeleft": 200,
                    }
                ]
            },
        )

    client = _mock_client(handler)
    result = RadarrQueueTool(client).run()
    assert result == [
        {
            "id": 9,
            "title": "The Matrix",
            "status": "downloading",
            "timeleft": "00:10:00",
            "size": 1000,
            "sizeleft": 200,
        }
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


# -- lookup_movie / radarr_lookup_movie


def test_lookup_movie_searches_tmdb_and_maps_fields() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v3/movie/lookup"
        assert request.url.params["term"] == "Dune 2021"
        return httpx.Response(
            200,
            json=[
                {
                    "tmdbId": 438631,
                    "title": "Dune",
                    "year": 2021,
                    "overview": "A mythic journey.",
                }
            ],
        )

    client = _mock_client(handler)
    result = RadarrLookupMovieTool(client).run(term="Dune 2021")
    assert result == [
        {
            "tmdb_id": 438631,
            "title": "Dune",
            "year": 2021,
            "overview": "A mythic journey.",
        }
    ]
    client.close()


# -- list_root_folders / radarr_list_root_folders


def test_list_root_folders_maps_fields_and_converts_free_space() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v3/rootfolder"
        return httpx.Response(
            200, json=[{"id": 1, "path": "/movies", "freeSpace": 5_000_000_000}]
        )

    client = _mock_client(handler)
    result = RadarrListRootFoldersTool(client).run()
    assert result == [{"id": 1, "path": "/movies", "free_space_gb": 5.0}]
    client.close()


# -- list_quality_profiles / radarr_list_quality_profiles


def test_list_quality_profiles_maps_fields() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v3/qualityprofile"
        return httpx.Response(200, json=[{"id": 4, "name": "HD-1080p"}])

    client = _mock_client(handler)
    result = RadarrListQualityProfilesTool(client).run()
    assert result == [{"id": 4, "name": "HD-1080p"}]
    client.close()


# -- add_movie / radarr_add_movie


def test_add_movie_posts_expected_payload_and_maps_the_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v3/movie"
        assert request.method == "POST"
        body = json.loads(request.content)
        assert body == {
            "tmdbId": 438631,
            "title": "Dune",
            "qualityProfileId": 4,
            "rootFolderPath": "/movies",
            "monitored": True,
            "addOptions": {"searchForMovie": True},
        }
        return httpx.Response(
            201, json={"id": 7, "title": "Dune", "year": 2021, "monitored": True}
        )

    client = _mock_client(handler)
    result = RadarrAddMovieTool(client).run(
        tmdb_id=438631, title="Dune", quality_profile_id=4, root_folder_path="/movies"
    )
    assert result == {"id": 7, "title": "Dune", "year": 2021, "monitored": True}
    client.close()


def test_add_movie_requires_confirmation() -> None:
    assert RadarrAddMovieTool.permission is ToolPermission.CONFIRM_REQUIRED


def test_add_movie_forwards_monitored_and_search_now_false() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["monitored"] is False
        assert body["addOptions"] == {"searchForMovie": False}
        return httpx.Response(
            201, json={"id": 8, "title": "Dune", "year": 2021, "monitored": False}
        )

    client = _mock_client(handler)
    RadarrAddMovieTool(client).run(
        tmdb_id=438631,
        title="Dune",
        quality_profile_id=4,
        root_folder_path="/movies",
        monitored=False,
        search_now=False,
    )
    client.close()


# -- verify(): result validation (milestone 69) -- the add can return
# success while Radarr silently fails to actually add the movie (e.g. an
# invalid root folder it accepts anyway), same "success doesn't mean it
# happened" gap DockerRestartContainerTool.verify() closes for restarts.


def test_add_movie_verify_reports_success_when_movie_appears_in_library() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {
                    "id": 7,
                    "title": "Dune",
                    "year": 2021,
                    "monitored": True,
                    "hasFile": False,
                    "status": "announced",
                }
            ],
        )

    client = _mock_client(handler)
    verification = RadarrAddMovieTool(client).verify({}, {"id": 7, "title": "Dune"})
    assert verification.verified is True
    assert "7" in verification.detail
    client.close()


def test_add_movie_verify_reports_failure_when_movie_never_appears() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    client = _mock_client(handler)
    verification = RadarrAddMovieTool(client).verify({}, {"id": 7, "title": "Dune"})
    assert verification.verified is False
    client.close()


def test_add_movie_verify_reports_failure_when_result_has_no_id() -> None:
    client = _mock_client(lambda r: httpx.Response(200, json=[]))
    verification = RadarrAddMovieTool(client).verify({}, {})
    assert verification.verified is False
    client.close()


# -- delete_movie / radarr_delete_movie


def test_delete_movie_sends_delete_files_param() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert request.url.path == "/api/v3/movie/7"
        assert request.url.params["deleteFiles"] == "true"
        return httpx.Response(200, json={})

    client = _mock_client(handler)
    result = RadarrDeleteMovieTool(client).run(movie_id=7, delete_files=True)
    assert result == {"deleted": True, "movie_id": 7, "files_deleted": True}
    client.close()


def test_delete_movie_requires_confirmation() -> None:
    assert RadarrDeleteMovieTool.permission is ToolPermission.CONFIRM_REQUIRED


def test_delete_movie_verify_reports_success_when_movie_is_gone() -> None:
    client = _mock_client(lambda r: httpx.Response(200, json=[]))
    verification = RadarrDeleteMovieTool(client).verify({"movie_id": 7}, {})
    assert verification.verified is True
    assert "7" in verification.detail
    client.close()


def test_delete_movie_verify_reports_failure_when_movie_still_present() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {
                    "id": 7,
                    "title": "Dune",
                    "year": 2021,
                    "monitored": True,
                    "hasFile": False,
                    "status": "announced",
                }
            ],
        )

    client = _mock_client(handler)
    verification = RadarrDeleteMovieTool(client).verify({"movie_id": 7}, {})
    assert verification.verified is False
    client.close()


def test_delete_movie_verify_raises_not_implemented_without_a_movie_id() -> None:
    client = _mock_client(lambda r: httpx.Response(200, json=[]))
    with pytest.raises(NotImplementedError):
        RadarrDeleteMovieTool(client).verify({}, {})
    client.close()


# -- calendar / radarr_calendar


def test_calendar_maps_release_dates() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v3/calendar"
        assert "start" in request.url.params
        assert "end" in request.url.params
        return httpx.Response(
            200,
            json=[
                {
                    "title": "Dune: Part Three",
                    "inCinemas": "2026-12-18",
                    "physicalRelease": None,
                    "digitalRelease": None,
                }
            ],
        )

    client = _mock_client(handler)
    result = RadarrCalendarTool(client).run(days_ahead=14)
    assert result == [
        {
            "title": "Dune: Part Three",
            "in_cinemas": "2026-12-18",
            "physical_release": None,
            "digital_release": None,
        }
    ]
    client.close()


# -- end-to-end confirmation gating via SirisAI.run_tool()


def test_add_movie_blocks_and_then_runs_once_confirmed() -> None:
    from unittest import mock

    from app.core.config import Settings
    from app.core.siris import SirisAI

    siris = SirisAI(
        settings=Settings(
            radarr_base_url="http://radarr.local:7878", radarr_api_key="secret"
        )
    )
    try:
        assert siris._radarr_client is not None
        with mock.patch.object(
            siris._radarr_client,
            "add_movie",
            return_value={"id": 7, "title": "Dune", "year": 2021, "monitored": True},
        ):
            with pytest.raises(ConfirmationRequired):
                siris.run_tool(
                    "radarr_add_movie",
                    {
                        "tmdb_id": 438631,
                        "title": "Dune",
                        "quality_profile_id": 4,
                        "root_folder_path": "/movies",
                    },
                )

            result = siris.run_tool(
                "radarr_add_movie",
                {
                    "tmdb_id": 438631,
                    "title": "Dune",
                    "quality_profile_id": 4,
                    "root_folder_path": "/movies",
                },
                confirmed=True,
            )
            assert result["id"] == 7
    finally:
        siris._radarr_client.close()


# -- remove_queue_item / radarr_remove_queue_item


def test_remove_queue_item_sends_remove_from_client_and_blocklist_params() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert request.url.path == "/api/v3/queue/9"
        assert request.url.params["removeFromClient"] == "true"
        assert request.url.params["blocklist"] == "true"
        return httpx.Response(200, json={})

    client = _mock_client(handler)
    result = RadarrRemoveQueueItemTool(client).run(queue_id=9, blocklist=True)
    assert result == {"removed": True, "queue_id": 9, "blocklisted": True}
    client.close()


def test_remove_queue_item_defaults_blocklist_to_false() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["blocklist"] == "false"
        return httpx.Response(200, json={})

    client = _mock_client(handler)
    RadarrRemoveQueueItemTool(client).run(queue_id=9)
    client.close()


def test_remove_queue_item_requires_confirmation() -> None:
    assert RadarrRemoveQueueItemTool.permission is ToolPermission.CONFIRM_REQUIRED


def test_remove_queue_item_verify_reports_success_when_item_is_gone() -> None:
    client = _mock_client(lambda r: httpx.Response(200, json={"records": []}))
    verification = RadarrRemoveQueueItemTool(client).verify({"queue_id": 9}, {})
    assert verification.verified is True
    assert "9" in verification.detail
    client.close()


def test_remove_queue_item_verify_reports_failure_when_item_still_present() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "records": [
                    {
                        "id": 9,
                        "title": "Dune",
                        "status": "downloading",
                        "timeleft": "00:10:00",
                    }
                ]
            },
        )

    client = _mock_client(handler)
    verification = RadarrRemoveQueueItemTool(client).verify({"queue_id": 9}, {})
    assert verification.verified is False
    client.close()


def test_remove_queue_item_verify_raises_not_implemented_without_a_queue_id() -> None:
    client = _mock_client(lambda r: httpx.Response(200, json=[]))
    with pytest.raises(NotImplementedError):
        RadarrRemoveQueueItemTool(client).verify({}, {})
    client.close()
