import httpx

from app.frigate.client import FrigateClient
from app.plugins.builtin.frigate import FrigateListCamerasTool, FrigateRecentEventsTool
from app.plugins.base import ToolPermission


def _mock_client(handler, api_key: str = "") -> FrigateClient:
    return FrigateClient(
        base_url="http://frigate.local:5000",
        api_key=api_key,
        transport=httpx.MockTransport(handler),
    )


# -- FrigateClient.list_cameras


def test_list_cameras_reads_config_and_reports_enabled_state() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/config"
        return httpx.Response(
            200,
            json={
                "cameras": {
                    "front_door": {"enabled": True},
                    "backyard": {"enabled": False},
                }
            },
        )

    client = _mock_client(handler)
    cameras = client.list_cameras()
    assert {"name": "front_door", "enabled": True} in cameras
    assert {"name": "backyard", "enabled": False} in cameras
    client.close()


def test_list_cameras_defaults_enabled_true_when_absent() -> None:
    client = _mock_client(
        lambda r: httpx.Response(200, json={"cameras": {"front_door": {}}})
    )
    cameras = client.list_cameras()
    assert cameras == [{"name": "front_door", "enabled": True}]
    client.close()


# -- FrigateClient.recent_events


def test_recent_events_maps_fields_and_builds_media_urls() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/events"
        assert request.url.params["limit"] == "10"
        return httpx.Response(
            200,
            json=[
                {
                    "id": "1726000000.123456-abcdef",
                    "camera": "front_door",
                    "label": "person",
                    "zones": ["porch"],
                    "top_score": 0.93,
                    "start_time": 1726000000.0,
                    "end_time": 1726000010.0,
                    "has_snapshot": True,
                    "has_clip": True,
                }
            ],
        )

    client = _mock_client(handler)
    events = client.recent_events()
    assert events == [
        {
            "id": "1726000000.123456-abcdef",
            "camera": "front_door",
            "label": "person",
            "zones": ["porch"],
            "top_score": 0.93,
            "start_time": "2024-09-10T20:26:40+00:00",
            "end_time": "2024-09-10T20:26:50+00:00",
            "snapshot_url": "http://frigate.local:5000/api/events/1726000000.123456-abcdef/snapshot.jpg",
            "clip_url": "http://frigate.local:5000/api/events/1726000000.123456-abcdef/clip.mp4",
        }
    ]
    client.close()


def test_recent_events_omits_media_urls_when_not_saved() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {
                    "id": "1",
                    "camera": "front_door",
                    "label": "person",
                    "start_time": 1726000000.0,
                    "end_time": None,
                    "has_snapshot": False,
                    "has_clip": False,
                }
            ],
        )

    client = _mock_client(handler)
    events = client.recent_events()
    assert events[0]["snapshot_url"] is None
    assert events[0]["clip_url"] is None
    assert events[0]["end_time"] is None
    client.close()


def test_recent_events_forwards_camera_and_label_filters() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["camera"] == "front_door"
        assert request.url.params["label"] == "person"
        assert request.url.params["limit"] == "5"
        return httpx.Response(200, json=[])

    client = _mock_client(handler)
    client.recent_events(camera="front_door", label="person", limit=5)
    client.close()


def test_client_sends_no_auth_header_when_no_api_key_given() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "authorization" not in request.headers
        return httpx.Response(200, json={"cameras": {}})

    client = _mock_client(handler, api_key="")
    client.list_cameras()
    client.close()


def test_client_sends_bearer_token_when_api_key_given() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer test-key"
        return httpx.Response(200, json={"cameras": {}})

    client = _mock_client(handler, api_key="test-key")
    client.list_cameras()
    client.close()


# -- Tools


def test_frigate_tools_are_read_permission() -> None:
    assert FrigateListCamerasTool.permission is ToolPermission.READ
    assert FrigateRecentEventsTool.permission is ToolPermission.READ


def test_list_cameras_tool_delegates_to_client() -> None:
    client = _mock_client(
        lambda r: httpx.Response(200, json={"cameras": {"front_door": {"enabled": True}}})
    )
    result = FrigateListCamerasTool(client).run()
    assert result == [{"name": "front_door", "enabled": True}]
    client.close()


def test_recent_events_tool_forwards_arguments_to_client() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["camera"] == "backyard"
        assert request.url.params["label"] == "dog"
        assert request.url.params["limit"] == "3"
        return httpx.Response(200, json=[])

    client = _mock_client(handler)
    result = FrigateRecentEventsTool(client).run(camera="backyard", label="dog", limit=3)
    assert result == []
    client.close()


def test_recent_events_tool_defaults_limit_to_ten() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["limit"] == "10"
        return httpx.Response(200, json=[])

    client = _mock_client(handler)
    FrigateRecentEventsTool(client).run()
    client.close()
