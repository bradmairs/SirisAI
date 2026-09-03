import httpx

from app.plugins.builtin.tautulli import TautulliCurrentActivityTool, TautulliRecentHistoryTool
from app.tautulli.client import TautulliClient


def _mock_client(handler) -> TautulliClient:
    return TautulliClient(base_url="http://tautulli.local:8181", api_key="test-key", transport=httpx.MockTransport(handler))


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
                            {"full_title": "The Matrix", "user": "brad", "state": "playing", "progress_percent": "42"}
                        ]
                    },
                }
            },
        )

    client = _mock_client(handler)
    result = TautulliCurrentActivityTool(client).run()
    assert result == [{"title": "The Matrix", "user": "brad", "state": "playing", "progress_percent": "42"}]
    client.close()


def test_recent_history_maps_rows_and_forwards_length() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["cmd"] == "get_history"
        assert request.url.params["length"] == "5"
        return httpx.Response(
            200,
            json={"response": {"result": "success", "data": {"data": [{"full_title": "Inception", "user": "brad", "date": "2026-01-01"}]}}},
        )

    client = _mock_client(handler)
    result = TautulliRecentHistoryTool(client).run(length=5)
    assert result == [{"title": "Inception", "user": "brad", "date": "2026-01-01"}]
    client.close()


def test_recent_history_defaults_length_to_ten() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["length"] == "10"
        return httpx.Response(200, json={"response": {"result": "success", "data": {"data": []}}})

    client = _mock_client(handler)
    TautulliRecentHistoryTool(client).run()
    client.close()
