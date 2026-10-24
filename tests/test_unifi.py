import json

import pytest
import httpx

from app.plugins.base import ToolPermission
from app.plugins.builtin.unifi import (
    UniFiBlockClientTool,
    UniFiListClientsTool,
    UniFiListDevicesTool,
    UniFiReconnectClientTool,
    UniFiResolveClientTool,
    UniFiRestartDeviceTool,
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
                    {
                        "id": "device-1",
                        "name": "Living Room AP",
                        "model": "U6-Lite",
                        "state": "ONLINE",
                    }
                ]
            },
        )

    client = _mock_client(handler)
    result = UniFiListDevicesTool(client).run()
    assert result == [
        {
            "id": "device-1",
            "name": "Living Room AP",
            "model": "U6-Lite",
            "state": "ONLINE",
        }
    ]
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


# -- reconnect_client


def test_reconnect_client_posts_reconnect_action() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/proxy/network/integration/v1/sites":
            return httpx.Response(200, json=_SITES_RESPONSE)
        assert request.method == "POST"
        assert (
            request.url.path
            == f"/proxy/network/integration/v1/sites/{_SITE_ID}/clients/client-1/actions"
        )
        assert json.loads(request.content) == {"action": "RECONNECT"}
        return httpx.Response(200, json={})

    client = _mock_client(handler)
    result = UniFiReconnectClientTool(client).run(client_id="client-1")
    assert result == {"reconnected": True, "client_id": "client-1"}
    client.close()


def test_reconnect_client_requires_confirmation() -> None:
    assert UniFiReconnectClientTool.permission is ToolPermission.CONFIRM_REQUIRED


def test_reconnect_client_has_no_verify() -> None:
    """No persistent flag to check afterward -- see the tool's own
    docstring for why this deliberately relies on Tool.verify()'s base
    NotImplementedError rather than adding one that couldn't mean
    anything."""
    client = _mock_client(lambda r: httpx.Response(200, json=_SITES_RESPONSE))
    tool = UniFiReconnectClientTool(client)
    with pytest.raises(NotImplementedError):
        tool.verify({"client_id": "client-1"}, {})
    client.close()


# -- restart_device


def _devices_handler(devices: list[dict]):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/proxy/network/integration/v1/sites":
            return httpx.Response(200, json=_SITES_RESPONSE)
        return httpx.Response(200, json={"data": devices})

    return handler


def test_restart_device_posts_restart_action() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/proxy/network/integration/v1/sites":
            return httpx.Response(200, json=_SITES_RESPONSE)
        assert request.method == "POST"
        assert (
            request.url.path
            == f"/proxy/network/integration/v1/sites/{_SITE_ID}/devices/device-1/actions"
        )
        assert json.loads(request.content) == {"action": "RESTART"}
        return httpx.Response(200, json={})

    client = _mock_client(handler)
    result = UniFiRestartDeviceTool(client).run(device_id="device-1")
    assert result == {"restarted": True, "device_id": "device-1"}
    client.close()


def test_restart_device_requires_confirmation() -> None:
    assert UniFiRestartDeviceTool.permission is ToolPermission.CONFIRM_REQUIRED


def test_restart_device_has_no_verify() -> None:
    """A full reboot takes far longer than any single post-call check
    could usefully wait for -- see the tool's own docstring for why
    this deliberately relies on Tool.verify()'s base NotImplementedError."""
    client = _mock_client(lambda r: httpx.Response(200, json=_SITES_RESPONSE))
    tool = UniFiRestartDeviceTool(client)
    with pytest.raises(NotImplementedError):
        tool.verify({"device_id": "device-1"}, {})
    client.close()


def test_restart_device_dry_run_reports_current_state() -> None:
    client = _mock_client(
        _devices_handler(
            [
                {
                    "id": "device-1",
                    "name": "Living Room AP",
                    "model": "U6-Lite",
                    "state": "ONLINE",
                }
            ]
        )
    )
    report = UniFiRestartDeviceTool(client).dry_run(device_id="device-1")
    assert report == {
        "device_id": "device-1",
        "name": "Living Room AP",
        "model": "U6-Lite",
        "current_state": "ONLINE",
        "would_restart": True,
    }
    assert "warning" not in report
    client.close()


def test_restart_device_dry_run_reports_unknown_state_when_not_found() -> None:
    client = _mock_client(_devices_handler([]))
    report = UniFiRestartDeviceTool(client).dry_run(device_id="device-1")
    assert report["current_state"] == "unknown (not found)"
    assert report["name"] is None
    client.close()


def test_restart_device_dry_run_warns_for_a_gateway_model() -> None:
    client = _mock_client(
        _devices_handler(
            [
                {
                    "id": "device-1",
                    "name": "Main Gateway",
                    "model": "UDM-Pro",
                    "state": "ONLINE",
                }
            ]
        )
    )
    report = UniFiRestartDeviceTool(client).dry_run(device_id="device-1")
    assert "warning" in report
    assert "UDM-Pro" in report["warning"]
    client.close()


def test_restart_device_dry_run_does_not_warn_for_a_switch_model() -> None:
    client = _mock_client(
        _devices_handler(
            [
                {
                    "id": "device-1",
                    "name": "Office Switch",
                    "model": "USW-24-PoE",
                    "state": "ONLINE",
                }
            ]
        )
    )
    report = UniFiRestartDeviceTool(client).dry_run(device_id="device-1")
    assert "warning" not in report
    client.close()


# ---------- live traffic for SirisOS's house view (2.76) ----------

_HEALTH = {"data": [{"subsystem": "wlan"}, {"subsystem": "wan", "rx_bytes-r": 1_250_000, "tx_bytes-r": 90_000, "isp_name": "Aussie Broadband"}]}
_DEVICES = {"data": [
    {"mac": "aa:00", "name": "Cloud Gateway Ultra", "model": "UCGULTRA", "type": "udm", "state": 1, "rx_bytes-r": 1_300_000, "tx_bytes-r": 95_000},
    {"mac": "aa:01", "name": "Office switch", "model": "USWFlexMini", "type": "usw", "state": 1, "uplink": {"uplink_mac": "aa:00"}, "rx_bytes-r": 20, "tx_bytes-r": 30},
    {"mac": "aa:02", "name": "Lounge AP", "model": "U7Pro", "type": "uap", "state": 0, "uplink": {"uplink_mac": "aa:01"}},
]}
_STA = {"data": [
    {"mac": "bb:01", "hostname": "gaming-pc", "ip": "192.168.0.20", "is_wired": True, "sw_mac": "aa:01", "wired-rx_bytes-r": 800_000, "wired-tx_bytes-r": 40_000},
    {"mac": "bb:02", "name": "Brad's iPhone", "is_wired": False, "ap_mac": "aa:02", "rx_bytes-r": 5_000, "tx_bytes-r": 1_000},
    {"mac": "bb:03", "oui": "Sonos", "is_wired": True, "sw_mac": "aa:00"},
]}


def _traffic_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path.endswith("/stat/health"):
        return httpx.Response(200, json=_HEALTH)
    if path.endswith("/stat/device"):
        return httpx.Response(200, json=_DEVICES)
    if path.endswith("/stat/sta"):
        return httpx.Response(200, json=_STA)
    return httpx.Response(404)


def test_traffic_reads_rates_and_what_connects_through_what() -> None:
    t = _mock_client(_traffic_handler).traffic()
    assert t["wan"] == {"rx_bps": 1_250_000.0, "tx_bps": 90_000.0, "isp": "Aussie Broadband"}
    assert [(d["name"], d["type"], d["state"]) for d in t["devices"]] == [
        ("Cloud Gateway Ultra", "gateway", "online"), ("Office switch", "switch", "online"), ("Lounge AP", "access_point", "offline")]
    pc, phone, sonos = t["clients"]
    assert (pc["name"], pc["wired"], pc["uplink_mac"], pc["rx_bps"]) == ("gaming-pc", True, "aa:01", 800_000.0)
    assert (phone["uplink_mac"], phone["tx_bps"]) == ("aa:02", 1_000.0)
    assert sonos["name"] == "Sonos" and sonos["rx_bps"] == 0.0


# A Cloud Gateway Ultra as it really answers: no device reports its own rx/tx_bytes-r, stat/health's
# "wan" reads 0 between refreshes, and the steady WAN rate is on the gateway's wan1 record.
_UCG_HEALTH = {"data": [{"subsystem": "wan", "rx_bytes-r": 0, "tx_bytes-r": 0, "isp_name": "Aussie Broadband"}]}
_UCG_DEVICES = {"data": [
    {"mac": "aa:00", "name": "UCG Ultra", "model": "UDRULT", "type": "udm", "state": 1,
     "wan1": {"rx_bytes-r": 62_000, "tx_bytes-r": 70_000, "rx_rate": 496_000}, "uplink": {"rx_bytes-r": 0, "tx_bytes-r": 0}},
    {"mac": "aa:01", "name": "Lite 8 PoE", "model": "USL8LPB", "type": "usw", "state": 1, "uplink": {"uplink_mac": "aa:00"},
     "port_table": [{"tx_bytes-r": 2_905_394, "rx_bytes-r": 218_073}]},
    {"mac": "aa:02", "name": "Hallway AP", "model": "UAPL6", "type": "uap", "state": 1, "uplink": {"uplink_mac": "aa:01", "rx_bytes-r": 14}},
    {"mac": "aa:03", "name": "Garage AP", "model": "UAPL6", "type": "uap", "state": 1, "uplink": {"uplink_mac": "aa:00"}},
    {"mac": "aa:04", "name": "Mesh AP", "model": "UAPL6", "type": "uap", "state": 1, "uplink": {"uplink_mac": "aa:02"}},
]}
_UCG_STA = {"data": [
    {"mac": "bb:01", "hostname": "server", "is_wired": True, "sw_mac": "aa:01", "wired-rx_bytes-r": 800_000, "wired-tx_bytes-r": 40_000},
    {"mac": "bb:02", "name": "Brad's iPhone", "is_wired": False, "ap_mac": "aa:02", "rx_bytes-r": 5_000, "tx_bytes-r": 1_000},
    {"mac": "bb:03", "name": "Laptop", "is_wired": False, "ap_mac": "aa:03", "rx_bytes-r": 2_000, "tx_bytes-r": 3_000},
    {"mac": "bb:04", "name": "Doorbell", "is_wired": False, "ap_mac": "aa:04", "rx_bytes-r": 1_000, "tx_bytes-r": 500},
]}


def _ucg_handler(health=_UCG_HEALTH, devices=_UCG_DEVICES, sta=_UCG_STA):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        for suffix, body in (("/stat/health", health), ("/stat/device", devices), ("/stat/sta", sta)):
            if path.endswith(suffix):
                return httpx.Response(200, json=body)
        return httpx.Response(404)
    return handler


def test_cloud_gateway_ultra_wan_comes_from_wan1_not_the_flickering_health_figure() -> None:
    t = _mock_client(_ucg_handler()).traffic()
    assert t["wan"] == {"rx_bps": 62_000.0, "tx_bps": 70_000.0, "isp": "Aussie Broadband"}


def test_devices_without_their_own_rates_carry_what_flows_through_them() -> None:
    by = {d["name"]: (d["rx_bps"], d["tx_bps"]) for d in _mock_client(_ucg_handler()).traffic()["devices"]}
    assert by["UCG Ultra"] == (62_000.0, 70_000.0)               # the gateway: the WAN line
    assert by["Mesh AP"] == (1_000.0, 500.0)                     # its one client
    assert by["Hallway AP"] == (6_000.0, 1_500.0)                # iPhone + the mesh AP below it
    assert by["Lite 8 PoE"] == (806_000.0, 41_500.0)             # server + the hallway AP's subtree
    assert by["Garage AP"] == (2_000.0, 3_000.0)                 # straight into the gateway


def test_a_second_wan_line_is_added_and_older_gateways_fall_back_to_health() -> None:
    devices = {"data": [{**_UCG_DEVICES["data"][0], "wan2": {"rx_bytes-r": 1_000, "tx_bytes-r": 2_000}}, *_UCG_DEVICES["data"][1:]]}
    assert _mock_client(_ucg_handler(devices=devices)).traffic()["wan"]["rx_bps"] == 63_000.0
    old = {"data": [{"mac": "aa:00", "name": "USG", "model": "UGW3", "type": "ugw", "state": 1}]}
    health = {"data": [{"subsystem": "wan", "rx_bytes-r": 9_000, "tx_bytes-r": 1_000, "isp_name": "ISP"}]}
    t = _mock_client(_ucg_handler(health=health, devices=old, sta={"data": []})).traffic()
    assert t["wan"]["rx_bps"] == 9_000.0 and t["devices"][0]["rx_bps"] == 9_000.0


def test_a_loop_in_reported_uplinks_does_not_hang() -> None:
    looped = {"data": [
        {"mac": "aa:00", "name": "GW", "type": "udm", "state": 1, "wan1": {"rx_bytes-r": 1, "tx_bytes-r": 1}},
        {"mac": "aa:05", "name": "AP one", "type": "uap", "state": 1, "uplink": {"uplink_mac": "aa:06"}},
        {"mac": "aa:06", "name": "AP two", "type": "uap", "state": 1, "uplink": {"uplink_mac": "aa:05"}},
    ]}
    sta = {"data": [{"mac": "bb:09", "name": "Phone", "is_wired": False, "ap_mac": "aa:05", "rx_bytes-r": 10, "tx_bytes-r": 20}]}
    by = {d["name"]: d["rx_bps"] for d in _mock_client(_ucg_handler(devices=looped, sta=sta)).traffic()["devices"]}
    # It finishes, and the phone's traffic is counted once; which AP of the loop also shows it
    # depends on the order, since a loop has no real "through".
    assert by["AP one"] == 10.0 and by["AP two"] in (0.0, 10.0)


def test_hub_network_shapes_the_graph(client) -> None:
    from app.core import guest
    from app.core.siris import get_siris
    from app.hub import contract as c
    from app.main import app
    from tests.test_hub import full_siris

    siris = full_siris()
    app.dependency_overrides[get_siris] = lambda: siris
    try:
        assert client.get("/siris/hub/v1/network").json()["configured"] is False
        siris._unifi_client = _mock_client(_traffic_handler)
        body = c.Network.model_validate(client.get("/siris/hub/v1/network").json())
        by = {n.name: n for n in body.nodes}
        assert by["Cloud Gateway Ultra"].kind == "gateway" and by["Cloud Gateway Ultra"].uplink is None
        assert by["Office switch"].uplink is None              # straight into the gateway
        assert by["Lounge AP"].uplink == "aa:01" and by["Lounge AP"].online is False
        assert by["gaming-pc"].uplink == "aa:01" and by["Sonos"].uplink is None and by["Brad's iPhone"].uplink == "aa:02"
        assert body.wan["isp"] == "Aussie Broadband"
        guest.set_mode(siris, True)
        assert {n.kind for n in c.Network.model_validate(client.get("/siris/hub/v1/network").json()).nodes} == {"gateway", "switch", "access_point"}
        guest.set_mode(siris, False)
    finally:
        app.dependency_overrides.pop(get_siris, None)
