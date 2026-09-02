import httpx

from app.plugins.builtin.unifi import UniFiListClientsTool, UniFiListDevicesTool
from app.unifi.client import UniFiClient


def _mock_client(handler) -> UniFiClient:
    return UniFiClient(
        base_url="https://unifi.local", api_key="test-key", site="default", transport=httpx.MockTransport(handler)
    )


def test_list_devices_hits_correct_path_and_unwraps_data_envelope() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/proxy/network/integration/v1/sites/default/devices"
        assert request.headers["x-api-key"] == "test-key"
        return httpx.Response(200, json={"data": [{"name": "Living Room AP", "model": "U6-Lite", "state": "online"}]})

    client = _mock_client(handler)
    result = UniFiListDevicesTool(client).run()
    assert result == [{"name": "Living Room AP", "model": "U6-Lite", "state": "online"}]
    client.close()


def test_list_clients_handles_bare_list_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/proxy/network/integration/v1/sites/default/clients"
        return httpx.Response(200, json=[{"name": "brads-laptop", "ipAddress": "192.168.1.50", "connectedAt": "t"}])

    client = _mock_client(handler)
    result = UniFiListClientsTool(client).run()
    assert result == [{"name": "brads-laptop", "ip_address": "192.168.1.50", "connected_at": "t"}]
    client.close()
