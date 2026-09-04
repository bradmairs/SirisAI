import pytest
import httpx

from app.plugins.builtin.unifi import UniFiListClientsTool, UniFiListDevicesTool
from app.unifi.client import UniFiClient

_SITES_RESPONSE = {
    "offset": 0,
    "limit": 200,
    "count": 1,
    "totalCount": 1,
    "data": [{"id": "88f7af54-98f8-306a-a1c7-c9349722b1f6", "internalReference": "default", "name": "Default"}],
}
_SITE_ID = _SITES_RESPONSE["data"][0]["id"]


def _mock_client(handler, site: str = "default") -> UniFiClient:
    return UniFiClient(
        base_url="https://192.168.1.1", api_key="test-key", site=site, transport=httpx.MockTransport(handler)
    )


def test_list_devices_resolves_site_id_then_hits_correct_path() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        assert request.headers["x-api-key"] == "test-key"
        if request.url.path == "/proxy/network/integration/v1/sites":
            return httpx.Response(200, json=_SITES_RESPONSE)
        assert request.url.path == f"/proxy/network/integration/v1/sites/{_SITE_ID}/devices"
        return httpx.Response(200, json={"data": [{"name": "Living Room AP", "model": "U6-Lite", "state": "ONLINE"}]})

    client = _mock_client(handler)
    result = UniFiListDevicesTool(client).run()
    assert result == [{"name": "Living Room AP", "model": "U6-Lite", "state": "ONLINE"}]
    assert calls == ["/proxy/network/integration/v1/sites", f"/proxy/network/integration/v1/sites/{_SITE_ID}/devices"]
    client.close()


def test_list_clients_reuses_cached_site_id_across_calls() -> None:
    site_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal site_calls
        if request.url.path == "/proxy/network/integration/v1/sites":
            site_calls += 1
            return httpx.Response(200, json=_SITES_RESPONSE)
        assert request.url.path == f"/proxy/network/integration/v1/sites/{_SITE_ID}/clients"
        return httpx.Response(
            200, json={"data": [{"name": "brads-laptop", "ipAddress": "192.168.1.50", "connectedAt": "t"}]}
        )

    client = _mock_client(handler)
    tool = UniFiListClientsTool(client)
    first = tool.run()
    second = tool.run()
    assert first == [{"name": "brads-laptop", "ip_address": "192.168.1.50", "connected_at": "t"}]
    assert second == first
    assert site_calls == 1  # resolved once, cached for the second call
    client.close()


def test_list_clients_handles_bare_list_response_for_devices_and_clients() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/proxy/network/integration/v1/sites":
            return httpx.Response(200, json=_SITES_RESPONSE)
        return httpx.Response(200, json=[{"name": "brads-laptop", "ipAddress": "192.168.1.50", "connectedAt": "t"}])

    client = _mock_client(handler)
    result = UniFiListClientsTool(client).run()
    assert result == [{"name": "brads-laptop", "ip_address": "192.168.1.50", "connected_at": "t"}]
    client.close()


def test_unknown_site_raises_a_clear_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_SITES_RESPONSE)

    client = _mock_client(handler, site="not-a-real-site")
    with pytest.raises(ValueError, match="not-a-real-site"):
        UniFiListDevicesTool(client).run()
    client.close()
