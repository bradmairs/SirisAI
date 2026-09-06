import httpx
import pytest

from app.plugins.builtin.tautulli import (
    TautulliCurrentActivityTool,
    TautulliGetLibrariesTool,
    TautulliGetUsersTool,
    TautulliHomeStatsTool,
    TautulliRecentHistoryTool,
    TautulliTerminateSessionTool,
)
from app.tautulli.client import TautulliClient


def _mock_client(handler) -> TautulliClient:
    return TautulliClient(
        base_url="http://tautulli.local:8181",
        api_key="test-key",
        transport=httpx.MockTransport(handler),
    )


def test_current_activity_maps_sessions_and_sends_apikey_and_cmd() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v2"
        assert request.url.params["apikey"] == "test-key"
        assert request.url.params["cmd"] == "get_activity"
        return httpx.Response(
            200,
            json={
                "response": {
                    "result": "success",
                    "data": {
                        "sessions": [
                            {
                                "session_id": "abc123",
                                "full_title": "The Matrix",
                                "user": "brad",
                                "state": "playing",
                                "progress_percent": "42",
                            }
                        ]
                    },
                }
            },
        )

    client = _mock_client(handler)
    result = TautulliCurrentActivityTool(client).run()
    assert result == [
        {
            "session_id": "abc123",
            "title": "The Matrix",
            "user": "brad",
            "state": "playing",
            "progress_percent": "42",
        }
    ]
    client.close()


def test_recent_history_maps_rows_and_forwards_length() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["cmd"] == "get_history"
        assert request.url.params["length"] == "5"
        return httpx.Response(
            200,
            json={
                "response": {
                    "result": "success",
                    "data": {
                        "data": [
                            {
                                "full_title": "Inception",
                                "user": "brad",
                                "date": "2026-01-01",
                            }
                        ]
                    },
                }
            },
        )

    client = _mock_client(handler)
    result = TautulliRecentHistoryTool(client).run(length=5)
    assert result == [{"title": "Inception", "user": "brad", "date": "2026-01-01"}]
    client.close()


def test_recent_history_defaults_length_to_ten() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["length"] == "10"
        return httpx.Response(
            200, json={"response": {"result": "success", "data": {"data": []}}}
        )

    client = _mock_client(handler)
    TautulliRecentHistoryTool(client).run()
    client.close()


# -- home_stats / tautulli_home_stats


def test_home_stats_maps_categories_and_forwards_days_and_count() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["cmd"] == "get_home_stats"
        assert request.url.params["time_range"] == "7"
        assert request.url.params["stats_count"] == "3"
        return httpx.Response(
            200,
            json={
                "response": {
                    "result": "success",
                    "data": [
                        {
                            "stat_id": "top_movies",
                            "stat_title": "Most Watched Movies",
                            "rows": [{"title": "The Matrix", "total_plays": 5}],
                        }
                    ],
                }
            },
        )

    client = _mock_client(handler)
    result = TautulliHomeStatsTool(client).run(days=7, count=3)
    assert result == [
        {
            "stat_id": "top_movies",
            "title": "Most Watched Movies",
            "rows": [{"title": "The Matrix", "total_plays": 5}],
        }
    ]
    client.close()


def test_home_stats_defaults_days_to_thirty_and_count_to_five() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["time_range"] == "30"
        assert request.url.params["stats_count"] == "5"
        return httpx.Response(200, json={"response": {"result": "success", "data": []}})

    client = _mock_client(handler)
    TautulliHomeStatsTool(client).run()
    client.close()


# -- terminate_session / tautulli_terminate_session


def test_terminate_session_sends_session_id_and_message() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["cmd"] == "terminate_session"
        assert request.url.params["session_id"] == "abc123"
        assert request.url.params["message"] == "Please free up the stream"
        return httpx.Response(
            200, json={"response": {"result": "success", "data": None}}
        )

    client = _mock_client(handler)
    result = TautulliTerminateSessionTool(client).run(
        session_id="abc123", message="Please free up the stream"
    )
    assert result == {"terminated": True, "session_id": "abc123"}
    client.close()


# -- verify(): same "the API call not raising doesn't prove it happened"
# gap this codebase's other verify()s close -- terminate_session is
# relayed to Plex, and the player client is what actually has to honor
# it, so Tautulli accepting the command doesn't mean the stream stopped.


def test_terminate_session_verify_reports_success_when_session_is_gone() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"response": {"result": "success", "data": {"sessions": []}}}
        )

    client = _mock_client(handler)
    verification = TautulliTerminateSessionTool(client).verify(
        {"session_id": "abc123"}, {}
    )
    assert verification.verified is True
    assert "abc123" in verification.detail
    client.close()


def test_terminate_session_verify_reports_failure_when_session_still_active() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "response": {
                    "result": "success",
                    "data": {
                        "sessions": [
                            {
                                "session_id": "abc123",
                                "full_title": "The Matrix",
                                "user": "brad",
                                "state": "playing",
                                "progress_percent": "50",
                            }
                        ]
                    },
                }
            },
        )

    client = _mock_client(handler)
    verification = TautulliTerminateSessionTool(client).verify(
        {"session_id": "abc123"}, {}
    )
    assert verification.verified is False
    client.close()


def test_terminate_session_verify_raises_not_implemented_without_a_session_id() -> None:
    client = _mock_client(
        lambda r: httpx.Response(200, json={"response": {"data": {}}})
    )
    with pytest.raises(NotImplementedError):
        TautulliTerminateSessionTool(client).verify({}, {})
    client.close()


# -- get_users / tautulli_get_users


def test_get_users_maps_fields() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["cmd"] == "get_users"
        return httpx.Response(
            200,
            json={
                "response": {
                    "result": "success",
                    "data": [
                        {"user_id": 1, "username": "brad", "friendly_name": "Brad"}
                    ],
                }
            },
        )

    client = _mock_client(handler)
    result = TautulliGetUsersTool(client).run()
    assert result == [{"user_id": 1, "username": "brad", "friendly_name": "Brad"}]
    client.close()


# -- get_libraries / tautulli_get_libraries


def test_get_libraries_maps_fields() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["cmd"] == "get_libraries"
        return httpx.Response(
            200,
            json={
                "response": {
                    "result": "success",
                    "data": [
                        {
                            "section_id": 1,
                            "section_name": "Movies",
                            "section_type": "movie",
                            "count": 250,
                        }
                    ],
                }
            },
        )

    client = _mock_client(handler)
    result = TautulliGetLibrariesTool(client).run()
    assert result == [
        {"section_id": 1, "name": "Movies", "type": "movie", "count": 250}
    ]
    client.close()
