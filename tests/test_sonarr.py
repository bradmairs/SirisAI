import json

import httpx
import pytest

from app.plugins.base import ConfirmationRequired, ToolPermission
from app.plugins.builtin.sonarr import (
    SonarrAddSeriesTool,
    SonarrCalendarTool,
    SonarrDeleteSeriesTool,
    SonarrListQualityProfilesTool,
    SonarrListRootFoldersTool,
    SonarrListSeriesTool,
    SonarrLookupSeriesTool,
    SonarrQueueTool,
    SonarrRemoveQueueItemTool,
    SonarrSearchSeriesTool,
)
from app.sonarr.client import SonarrClient


def _mock_client(handler) -> SonarrClient:
    return SonarrClient(
        base_url="http://sonarr.local:8989",
        api_key="test-key",
        transport=httpx.MockTransport(handler),
    )


def test_list_series_maps_fields_and_sends_api_key_header() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v3/series"
        assert request.headers["x-api-key"] == "test-key"
        return httpx.Response(
            200,
            json=[
                {
                    "id": 1,
                    "title": "Breaking Bad",
                    "year": 2008,
                    "monitored": True,
                    "status": "ended",
                }
            ],
        )

    client = _mock_client(handler)
    result = SonarrListSeriesTool(client).run()
    assert result == [
        {
            "id": 1,
            "title": "Breaking Bad",
            "year": 2008,
            "monitored": True,
            "status": "ended",
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
                        "id": 3,
                        "title": "Breaking Bad S01E01",
                        "status": "downloading",
                        "timeleft": "00:05:00",
                        "size": 500,
                        "sizeleft": 50,
                    }
                ]
            },
        )

    client = _mock_client(handler)
    result = SonarrQueueTool(client).run()
    assert result == [
        {
            "id": 3,
            "title": "Breaking Bad S01E01",
            "status": "downloading",
            "timeleft": "00:05:00",
            "size": 500,
            "sizeleft": 50,
        }
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


# -- lookup_series / sonarr_lookup_series


def test_lookup_series_searches_tvdb_and_maps_fields() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v3/series/lookup"
        assert request.url.params["term"] == "Severance"
        return httpx.Response(
            200,
            json=[
                {
                    "tvdbId": 371980,
                    "title": "Severance",
                    "year": 2022,
                    "overview": "A workplace thriller.",
                }
            ],
        )

    client = _mock_client(handler)
    result = SonarrLookupSeriesTool(client).run(term="Severance")
    assert result == [
        {
            "tvdb_id": 371980,
            "title": "Severance",
            "year": 2022,
            "overview": "A workplace thriller.",
        }
    ]
    client.close()


# -- list_root_folders / sonarr_list_root_folders


def test_list_root_folders_maps_fields_and_converts_free_space() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v3/rootfolder"
        return httpx.Response(
            200, json=[{"id": 1, "path": "/tv", "freeSpace": 5_000_000_000}]
        )

    client = _mock_client(handler)
    result = SonarrListRootFoldersTool(client).run()
    assert result == [{"id": 1, "path": "/tv", "free_space_gb": 5.0}]
    client.close()


# -- list_quality_profiles / sonarr_list_quality_profiles


def test_list_quality_profiles_maps_fields() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v3/qualityprofile"
        return httpx.Response(200, json=[{"id": 4, "name": "HD-1080p"}])

    client = _mock_client(handler)
    result = SonarrListQualityProfilesTool(client).run()
    assert result == [{"id": 4, "name": "HD-1080p"}]
    client.close()


# -- add_series / sonarr_add_series


def test_add_series_posts_expected_payload_and_maps_the_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v3/series"
        assert request.method == "POST"
        body = json.loads(request.content)
        assert body == {
            "tvdbId": 371980,
            "title": "Severance",
            "qualityProfileId": 4,
            "rootFolderPath": "/tv",
            "monitored": True,
            "seasonFolder": True,
            "addOptions": {"searchForMissingEpisodes": True},
        }
        return httpx.Response(
            201, json={"id": 7, "title": "Severance", "year": 2022, "monitored": True}
        )

    client = _mock_client(handler)
    result = SonarrAddSeriesTool(client).run(
        tvdb_id=371980, title="Severance", quality_profile_id=4, root_folder_path="/tv"
    )
    assert result == {"id": 7, "title": "Severance", "year": 2022, "monitored": True}
    client.close()


def test_add_series_requires_confirmation() -> None:
    assert SonarrAddSeriesTool.permission is ToolPermission.CONFIRM_REQUIRED


def test_add_series_forwards_monitored_and_search_now_false() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["monitored"] is False
        assert body["addOptions"] == {"searchForMissingEpisodes": False}
        return httpx.Response(
            201, json={"id": 8, "title": "Severance", "year": 2022, "monitored": False}
        )

    client = _mock_client(handler)
    SonarrAddSeriesTool(client).run(
        tvdb_id=371980,
        title="Severance",
        quality_profile_id=4,
        root_folder_path="/tv",
        monitored=False,
        search_now=False,
    )
    client.close()


# -- verify(): result validation (milestone 69) -- same "success doesn't
# mean it happened" gap RadarrAddMovieTool.verify() closes for movies.


def test_add_series_verify_reports_success_when_series_appears_in_library() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {
                    "id": 7,
                    "title": "Severance",
                    "year": 2022,
                    "monitored": True,
                    "status": "continuing",
                }
            ],
        )

    client = _mock_client(handler)
    verification = SonarrAddSeriesTool(client).verify(
        {}, {"id": 7, "title": "Severance"}
    )
    assert verification.verified is True
    assert "7" in verification.detail
    client.close()


def test_add_series_verify_reports_failure_when_series_never_appears() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    client = _mock_client(handler)
    verification = SonarrAddSeriesTool(client).verify(
        {}, {"id": 7, "title": "Severance"}
    )
    assert verification.verified is False
    client.close()


def test_add_series_verify_reports_failure_when_result_has_no_id() -> None:
    client = _mock_client(lambda r: httpx.Response(200, json=[]))
    verification = SonarrAddSeriesTool(client).verify({}, {})
    assert verification.verified is False
    client.close()


# -- delete_series / sonarr_delete_series


def test_delete_series_sends_delete_files_param() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert request.url.path == "/api/v3/series/7"
        assert request.url.params["deleteFiles"] == "true"
        return httpx.Response(200, json={})

    client = _mock_client(handler)
    result = SonarrDeleteSeriesTool(client).run(series_id=7, delete_files=True)
    assert result == {"deleted": True, "series_id": 7, "files_deleted": True}
    client.close()


def test_delete_series_requires_confirmation() -> None:
    assert SonarrDeleteSeriesTool.permission is ToolPermission.CONFIRM_REQUIRED


def test_delete_series_verify_reports_success_when_series_is_gone() -> None:
    client = _mock_client(lambda r: httpx.Response(200, json=[]))
    verification = SonarrDeleteSeriesTool(client).verify({"series_id": 7}, {})
    assert verification.verified is True
    assert "7" in verification.detail
    client.close()


def test_delete_series_verify_reports_failure_when_series_still_present() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[{"id": 7, "title": "Severance", "year": 2022, "monitored": True}],
        )

    client = _mock_client(handler)
    verification = SonarrDeleteSeriesTool(client).verify({"series_id": 7}, {})
    assert verification.verified is False
    client.close()


def test_delete_series_verify_raises_not_implemented_without_a_series_id() -> None:
    client = _mock_client(lambda r: httpx.Response(200, json=[]))
    with pytest.raises(NotImplementedError):
        SonarrDeleteSeriesTool(client).verify({}, {})
    client.close()


# -- calendar / sonarr_calendar


def test_calendar_maps_episode_fields_and_includes_series() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v3/calendar"
        assert "start" in request.url.params
        assert "end" in request.url.params
        assert request.url.params["includeSeries"] == "true"
        return httpx.Response(
            200,
            json=[
                {
                    "series": {"title": "Severance"},
                    "title": "Cold Harbor",
                    "seasonNumber": 2,
                    "episodeNumber": 1,
                    "airDate": "2026-01-17",
                }
            ],
        )

    client = _mock_client(handler)
    result = SonarrCalendarTool(client).run(days_ahead=14)
    assert result == [
        {
            "series_title": "Severance",
            "episode_title": "Cold Harbor",
            "season_number": 2,
            "episode_number": 1,
            "air_date": "2026-01-17",
        }
    ]
    client.close()


# -- end-to-end confirmation gating via SirisAI.run_tool()


def test_add_series_blocks_and_then_runs_once_confirmed() -> None:
    from unittest import mock

    from app.core.config import Settings
    from app.core.siris import SirisAI

    siris = SirisAI(
        settings=Settings(
            sonarr_base_url="http://sonarr.local:8989", sonarr_api_key="secret"
        )
    )
    try:
        assert siris._sonarr_client is not None
        with mock.patch.object(
            siris._sonarr_client,
            "add_series",
            return_value={
                "id": 7,
                "title": "Severance",
                "year": 2022,
                "monitored": True,
            },
        ):
            with pytest.raises(ConfirmationRequired):
                siris.run_tool(
                    "sonarr_add_series",
                    {
                        "tvdb_id": 371980,
                        "title": "Severance",
                        "quality_profile_id": 4,
                        "root_folder_path": "/tv",
                    },
                )

            result = siris.run_tool(
                "sonarr_add_series",
                {
                    "tvdb_id": 371980,
                    "title": "Severance",
                    "quality_profile_id": 4,
                    "root_folder_path": "/tv",
                },
                confirmed=True,
            )
            assert result["id"] == 7
    finally:
        siris._sonarr_client.close()


# -- remove_queue_item / sonarr_remove_queue_item


def test_remove_queue_item_sends_remove_from_client_and_blocklist_params() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert request.url.path == "/api/v3/queue/3"
        assert request.url.params["removeFromClient"] == "true"
        assert request.url.params["blocklist"] == "true"
        return httpx.Response(200, json={})

    client = _mock_client(handler)
    result = SonarrRemoveQueueItemTool(client).run(queue_id=3, blocklist=True)
    assert result == {"removed": True, "queue_id": 3, "blocklisted": True}
    client.close()


def test_remove_queue_item_defaults_blocklist_to_false() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["blocklist"] == "false"
        return httpx.Response(200, json={})

    client = _mock_client(handler)
    SonarrRemoveQueueItemTool(client).run(queue_id=3)
    client.close()


def test_remove_queue_item_requires_confirmation() -> None:
    assert SonarrRemoveQueueItemTool.permission is ToolPermission.CONFIRM_REQUIRED


def test_remove_queue_item_verify_reports_success_when_item_is_gone() -> None:
    client = _mock_client(lambda r: httpx.Response(200, json={"records": []}))
    verification = SonarrRemoveQueueItemTool(client).verify({"queue_id": 3}, {})
    assert verification.verified is True
    assert "3" in verification.detail
    client.close()


def test_remove_queue_item_verify_reports_failure_when_item_still_present() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "records": [
                    {
                        "id": 3,
                        "title": "Severance",
                        "status": "downloading",
                        "timeleft": "00:05:00",
                    }
                ]
            },
        )

    client = _mock_client(handler)
    verification = SonarrRemoveQueueItemTool(client).verify({"queue_id": 3}, {})
    assert verification.verified is False
    client.close()


def test_remove_queue_item_verify_raises_not_implemented_without_a_queue_id() -> None:
    client = _mock_client(lambda r: httpx.Response(200, json=[]))
    with pytest.raises(NotImplementedError):
        SonarrRemoveQueueItemTool(client).verify({}, {})
    client.close()
