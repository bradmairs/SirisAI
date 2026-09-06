import json

import pytest
import httpx

from app.plugins.base import ToolPermission
from app.plugins.builtin.unifi import (
    UniFiBlockClientTool,
    UniFiListClientsTool,
    UniFiListDevicesTool,
    UniFiResolveClientTool,
    UniFiUnblockClientTool,
)
from app.unifi.client import UniFiClient

_SITES_RESPONSE = {
    "offset": 0,
    "limit": 200,
    "count": 1,
    "totalCount": 1,
    "data": [
        {
            "id": "88f7af54-98f8-306a-a1c7-c9349722b1f6",
            "internalReference": "default",
            "name": "Default",
        }
    ],
}
_SITE_ID = _SITES_RESPONSE["data"][0]["id"]


def _mock_client(handler, site: str = "default") -> UniFiClient:
    return UniFiClient(
        base_url="https://192.168.1.1",
        api_key="test-key",
        site=site,
        transport=httpx.MockTransport(handler),
    )


def test_list_devices_resolves_site_id_then_hits_correct_path() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        assert request.headers["x-api-key"] == "test-key"
        if request.url.path == "/proxy/network/integration/v1/sites":
            return httpx.Response(200, json=_SITES_RESPONSE)
        assert (
            request.url.path
            == f"/proxy/network/integration/v1/sites/{_SITE_ID}/devices"
        )
        return httpx.Response(
            200,
            json={
                "data": [
                    {"name": "Living Room AP", "model": "U6-Lite", "state": "ONLINE"}
                ]
            },
        )

    client = _mock_client(handler)
    result = UniFiListDevicesTool(client).run()
    assert result == [{"name": "Living Room AP", "model": "U6-Lite", "state": "ONLINE"}]
    assert calls == [
        "/proxy/network/integration/v1/sites",
        f"/proxy/network/integration/v1/sites/{_SITE_ID}/devices",
    ]
    client.close()


def test_list_clients_reuses_cached_site_id_across_calls() -> None:
    site_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal site_calls
        if request.url.path == "/proxy/network/integration/v1/sites":
            site_calls += 1
            return httpx.Response(200, json=_SITES_RESPONSE)
        assert (
            request.url.path
            == f"/proxy/network/integration/v1/sites/{_SITE_ID}/clients"
        )
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "name": "brads-laptop",
                        "ipAddress": "192.168.1.50",
                        "connectedAt": "t",
                    }
                ]
            },
        )

    client = _mock_client(handler)
    tool = UniFiListClientsTool(client)
    first = tool.run()
    second = tool.run()
    assert first == [
        {
            "id": None,
            "name": "brads-laptop",
            "ip_address": "192.168.1.50",
            "connected_at": "t",
            "blocked": None,
        }
    ]
    assert second == first
    assert site_calls == 1  # resolved once, cached for the second call
    client.close()


def test_list_clients_handles_bare_list_response_for_devices_and_clients() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/proxy/network/integration/v1/sites":
            return httpx.Response(200, json=_SITES_RESPONSE)
        return httpx.Response(
            200,
            json=[
                {
                    "name": "brads-laptop",
                    "ipAddress": "192.168.1.50",
                    "connectedAt": "t",
                }
            ],
        )

    client = _mock_client(handler)
    result = UniFiListClientsTool(client).run()
    assert result == [
        {
            "id": None,
            "name": "brads-laptop",
            "ip_address": "192.168.1.50",
            "connected_at": "t",
            "blocked": None,
        }
    ]
    client.close()


def test_unknown_site_raises_a_clear_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_SITES_RESPONSE)

    client = _mock_client(handler, site="not-a-real-site")
    with pytest.raises(ValueError, match="not-a-real-site"):
        UniFiListDevicesTool(client).run()
    client.close()


# -- resolve_client


def _clients_handler(clients: list[dict]):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/proxy/network/integration/v1/sites":
            return httpx.Response(200, json=_SITES_RESPONSE)
        return httpx.Response(200, json={"data": clients})

    return handler


def test_resolve_client_matches_by_name() -> None:
    client = _mock_client(
        _clients_handler(
            [
                {"id": "c1", "name": "kids-iphone", "ipAddress": "192.168.1.20"},
                {"id": "c2", "name": "kitchen-tv", "ipAddress": "192.168.1.30"},
            ]
        )
    )
    result = UniFiResolveClientTool(client).run(name_hint="kids phone")
    assert result["ambiguous"] is False
    assert [m["id"] for m in result["matches"]] == ["c1"]
    client.close()


def test_resolve_client_matches_by_ip_address() -> None:
    client = _mock_client(
        _clients_handler(
            [{"id": "c1", "name": "kids-iphone", "ipAddress": "192.168.1.20"}]
        )
    )
    result = UniFiResolveClientTool(client).run(name_hint="192.168.1.20")
    assert result["ambiguous"] is False
    assert result["matches"][0]["id"] == "c1"
    client.close()


def test_resolve_client_flags_ambiguous_for_multiple_matches() -> None:
    client = _mock_client(
        _clients_handler(
            [
                {"id": "c1", "name": "kitchen-tv", "ipAddress": "192.168.1.20"},
                {"id": "c2", "name": "kitchen-echo", "ipAddress": "192.168.1.21"},
            ]
        )
    )
    result = UniFiResolveClientTool(client).run(name_hint="kitchen")
    assert result["ambiguous"] is True
    assert len(result["matches"]) == 2
    client.close()


def test_resolve_client_returns_no_matches_when_nothing_fits() -> None:
    client = _mock_client(
        _clients_handler(
            [{"id": "c1", "name": "kids-iphone", "ipAddress": "192.168.1.20"}]
        )
    )
    result = UniFiResolveClientTool(client).run(name_hint="nonexistent")
    assert result == {"matches": [], "ambiguous": False}
    client.close()


# -- block_client / unblock_client


def test_block_client_posts_block_action() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/proxy/network/integration/v1/sites":
            return httpx.Response(200, json=_SITES_RESPONSE)
        assert request.method == "POST"
        assert (
            request.url.path
            == f"/proxy/network/integration/v1/sites/{_SITE_ID}/clients/client-1/actions"
        )
        assert json.loads(request.content) == {"action": "BLOCK"}
        return httpx.Response(200, json={})

    client = _mock_client(handler)
    result = UniFiBlockClientTool(client).run(client_id="client-1")
    assert result == {"blocked": True, "client_id": "client-1"}
    client.close()


def test_unblock_client_posts_unblock_action() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/proxy/network/integration/v1/sites":
            return httpx.Response(200, json=_SITES_RESPONSE)
        assert json.loads(request.content) == {"action": "UNBLOCK"}
        return httpx.Response(200, json={})

    client = _mock_client(handler)
    result = UniFiUnblockClientTool(client).run(client_id="client-1")
    assert result == {"blocked": False, "client_id": "client-1"}
    client.close()


def test_block_client_requires_confirmation() -> None:
    assert UniFiBlockClientTool.permission is ToolPermission.CONFIRM_REQUIRED


def test_unblock_client_requires_confirmation() -> None:
    assert UniFiUnblockClientTool.permission is ToolPermission.CONFIRM_REQUIRED


# -- verify(): same "the API call not raising doesn't prove it happened"
# gap this codebase's other verify()s close.


def test_block_client_verify_reports_success_when_client_shows_blocked() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/proxy/network/integration/v1/sites":
            return httpx.Response(200, json=_SITES_RESPONSE)
        return httpx.Response(
            200,
            json={"data": [{"id": "client-1", "name": "kid-phone", "blocked": True}]},
        )

    client = _mock_client(handler)
    verification = UniFiBlockClientTool(client).verify({"client_id": "client-1"}, {})
    assert verification.verified is True
    assert "client-1" in verification.detail
    client.close()


def test_block_client_verify_reports_failure_when_client_not_blocked() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/proxy/network/integration/v1/sites":
            return httpx.Response(200, json=_SITES_RESPONSE)
        return httpx.Response(
            200,
            json={"data": [{"id": "client-1", "name": "kid-phone", "blocked": False}]},
        )

    client = _mock_client(handler)
    verification = UniFiBlockClientTool(client).verify({"client_id": "client-1"}, {})
    assert verification.verified is False
    client.close()


def test_block_client_verify_raises_not_implemented_when_client_not_found() -> None:
    """Genuinely ambiguous without a real controller to confirm against
    -- see UniFiBlockClientTool.verify()'s own docstring."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/proxy/network/integration/v1/sites":
            return httpx.Response(200, json=_SITES_RESPONSE)
        return httpx.Response(200, json={"data": []})

    client = _mock_client(handler)
    with pytest.raises(NotImplementedError):
        UniFiBlockClientTool(client).verify({"client_id": "client-1"}, {})
    client.close()


def test_block_client_verify_raises_not_implemented_without_a_client_id() -> None:
    client = _mock_client(lambda r: httpx.Response(200, json=_SITES_RESPONSE))
    with pytest.raises(NotImplementedError):
        UniFiBlockClientTool(client).verify({}, {})
    client.close()


def test_unblock_client_verify_reports_success_when_client_not_blocked() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/proxy/network/integration/v1/sites":
            return httpx.Response(200, json=_SITES_RESPONSE)
        return httpx.Response(
            200,
            json={"data": [{"id": "client-1", "name": "kid-phone", "blocked": False}]},
        )

    client = _mock_client(handler)
    verification = UniFiUnblockClientTool(client).verify({"client_id": "client-1"}, {})
    assert verification.verified is True
    client.close()


def test_unblock_client_verify_reports_success_when_client_not_found() -> None:
    """Unlike blocking, absence here doesn't contradict success -- it
    just means nothing has reconnected yet."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/proxy/network/integration/v1/sites":
            return httpx.Response(200, json=_SITES_RESPONSE)
        return httpx.Response(200, json={"data": []})

    client = _mock_client(handler)
    verification = UniFiUnblockClientTool(client).verify({"client_id": "client-1"}, {})
    assert verification.verified is True
    client.close()


def test_unblock_client_verify_reports_failure_when_client_still_blocked() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/proxy/network/integration/v1/sites":
            return httpx.Response(200, json=_SITES_RESPONSE)
        return httpx.Response(
            200,
            json={"data": [{"id": "client-1", "name": "kid-phone", "blocked": True}]},
        )

    client = _mock_client(handler)
    verification = UniFiUnblockClientTool(client).verify({"client_id": "client-1"}, {})
    assert verification.verified is False
    client.close()


def test_unblock_client_verify_raises_not_implemented_without_a_client_id() -> None:
    client = _mock_client(lambda r: httpx.Response(200, json=_SITES_RESPONSE))
    with pytest.raises(NotImplementedError):
        UniFiUnblockClientTool(client).verify({}, {})
    client.close()
