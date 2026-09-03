import pytest
import httpx

from app.plugins.builtin.unifi import UniFiListClientsTool, UniFiListDevicesTool
from app.unifi.client import UniFiClient


def _mock_client(handler) -> UniFiClient:
    return UniFiClient(
        base_url="https://api.ui.com", api_key="test-key", site="default", transport=httpx.MockTransport(handler)
    )


def test_list_devices_hits_correct_path_and_flattens_devices_across_hosts() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/devices"
        assert request.headers["x-api-key"] == "test-key"
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "hostId": "host-1",
                        "hostName": "Cloud Gateway Ultra",
                        "devices": [
                            {"name": "Living Room AP", "model": "U6-Lite", "status": "online"},
                            {"name": "Office Switch", "model": "USW-Lite-8-PoE", "status": "online"},
                        ],
                    }
                ]
            },
        )

    client = _mock_client(handler)
    result = UniFiListDevicesTool(client).run()
    assert result == [
        {"name": "Living Room AP", "model": "U6-Lite", "state": "online"},
        {"name": "Office Switch", "model": "USW-Lite-8-PoE", "state": "online"},
    ]
    client.close()


def test_list_clients_raises_not_implemented_since_the_cloud_api_has_no_client_endpoint() -> None:
    client = _mock_client(lambda r: httpx.Response(200, json={"data": []}))
    with pytest.raises(NotImplementedError):
        UniFiListClientsTool(client).run()
    client.close()
