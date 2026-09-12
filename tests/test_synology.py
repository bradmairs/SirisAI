"""Tests for Synology NAS status tool."""

import json
import pytest
import httpx

from app.plugins.builtin.synology import SynologyGetStatusTool
from app.synology.client import SynologyClient


# --- Mock responses ---

_SID_RESPONSE = {"success": True, "data": {"sid": "test-sid-123"}}

_SYSTEM_STATUS_RESPONSE = {
    "success": True,
    "data": {
        "model": "DS920+",
        "serial": "1234567890",
        "temperature": 35,
        "fan_status": "normal",
        "cpu_usage": 15,
        "memory_usage": 45,
    },
}

_STORAGE_INFO_RESPONSE = {
    "success": True,
    "data": {
        "volumes": [
            {
                "id": "volume_1",
                "name": "Volume 1",
                "status": "normal",
                "total_size": 1099511627776,  # 1 TB
                "used_size": 549755813888,  # 500 GB
                "disk_ids": ["disk_1", "disk_2"],
            }
        ],
        "disks": [
            {
                "id": "disk_1",
                "name": "/dev/sda",
                "model": "WDC WD40EFRX-68WT0N0",
                "status": "normal",
                "temperature": 32,
                "size": 4000787030016,
            },
            {
                "id": "disk_2",
                "name": "/dev/sdb",
                "model": "WDC WD40EFRX-68WT0N0",
                "status": "normal",
                "temperature": 33,
                "size": 4000787030016,
            },
        ],
    },
}

_DISK_SMART_RESPONSE = {
    "success": True,
    "data": {
        "disks": [
            {
                "disk_id": "disk_1",
                "smart_info": {
                    "overall_health": "PASSED",
                    "attributes": [
                        {"id": 5, "name": "Reallocated_Sector_Ct", "value": 100, "worst": 100, "threshold": 10, "raw": 0},
                        {"id": 197, "name": "Current_Pending_Sector", "value": 100, "worst": 100, "threshold": 0, "raw": 0},
                    ],
                },
            },
            {
                "disk_id": "disk_2",
                "smart_info": {
                    "overall_health": "PASSED",
                    "attributes": [
                        {"id": 5, "name": "Reallocated_Sector_Ct", "value": 100, "worst": 100, "threshold": 10, "raw": 0},
                        {"id": 197, "name": "Current_Pending_Sector", "value": 100, "worst": 100, "threshold": 0, "raw": 0},
                    ],
                },
            },
        ]
    },
}

_BACKUP_TASKS_RESPONSE = {
    "success": True,
    "data": {
        "tasks": [
            {
                "id": "backup_1",
                "name": "Photos Backup",
                "status": "completed",
                "last_run": "2026-09-12T02:00:00+10:00",
                "next_run": "2026-09-13T02:00:00+10:00",
                "progress": 100,
            },
            {
                "id": "backup_2",
                "name": "Documents Backup",
                "status": "running",
                "last_run": "2026-09-12T03:00:00+10:00",
                "next_run": "2026-09-13T03:00:00+10:00",
                "progress": 45,
            },
        ]
    },
}


def _mock_synology_client() -> SynologyClient:
    """Create a SynologyClient with a mock transport that simulates all API calls."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.url.path, dict(request.url.params)))
        params = dict(request.url.params)

        # Login
        if params.get("api") == "SYNO.API.Auth" and params.get("method") == "login":
            return httpx.Response(200, json=_SID_RESPONSE)

        # System status
        if params.get("api") == "SYNO.Core.System" and params.get("method") == "get":
            assert params.get("_sid") == "test-sid-123"
            return httpx.Response(200, json=_SYSTEM_STATUS_RESPONSE)

        # Storage info
        if params.get("api") == "SYNO.Core.Storage" and params.get("method") == "get":
            assert params.get("_sid") == "test-sid-123"
            return httpx.Response(200, json=_STORAGE_INFO_RESPONSE)

        # Disk SMART info
        if params.get("api") == "SYNO.Core.Storage" and params.get("method") == "disk_smart_info_get":
            assert params.get("_sid") == "test-sid-123"
            return httpx.Response(200, json=_DISK_SMART_RESPONSE)

        # Backup tasks (try SYNO.Backup.Task first)
        if params.get("api") == "SYNO.Backup.Task" and params.get("method") == "list":
            assert params.get("_sid") == "test-sid-123"
            return httpx.Response(200, json=_BACKUP_TASKS_RESPONSE)

        # Logout
        if params.get("api") == "SYNO.API.Auth" and params.get("method") == "logout":
            return httpx.Response(200, json={"success": True})

        return httpx.Response(404, json={"error": "Not found"})

    return SynologyClient(
        base_url="https://192.168.1.100",
        username="admin",
        password="password",
        transport=httpx.MockTransport(handler),
    )


def test_synology_get_status_returns_all_sections() -> None:
    """Test that the tool returns system, storage, disk_smart, and backup_tasks."""
    client = _mock_synology_client()
    tool = SynologyGetStatusTool(client)

    result = tool.run()

    assert "system" in result
    assert "storage" in result
    assert "disk_smart" in result
    assert "backup_tasks" in result

    # Verify system status
    assert result["system"]["model"] == "DS920+"
    assert result["system"]["serial"] == "1234567890"
    assert result["system"]["temperature"] == 35

    # Verify storage info
    assert len(result["storage"]["volumes"]) == 1
    assert result["storage"]["volumes"][0]["name"] == "Volume 1"
    assert len(result["storage"]["disks"]) == 2

    # Verify disk SMART
    assert len(result["disk_smart"]) == 2
    assert result["disk_smart"][0]["disk_id"] == "disk_1"
    assert result["disk_smart"][0]["smart_info"]["overall_health"] == "PASSED"

    # Verify backup tasks
    assert len(result["backup_tasks"]) == 2
    assert result["backup_tasks"][0]["name"] == "Photos Backup"
    assert result["backup_tasks"][0]["status"] == "completed"
    assert result["backup_tasks"][1]["status"] == "running"


def test_synology_client_login_logout() -> None:
    """Test that SynologyClient handles login and logout correctly."""
    login_called = False
    logout_called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal login_called, logout_called
        params = dict(request.url.params)

        if params.get("api") == "SYNO.API.Auth" and params.get("method") == "login":
            login_called = True
            return httpx.Response(200, json=_SID_RESPONSE)

        if params.get("api") == "SYNO.API.Auth" and params.get("method") == "logout":
            logout_called = True
            return httpx.Response(200, json={"success": True})

        return httpx.Response(404, json={"error": "Not found"})

    client = SynologyClient(
        base_url="https://192.168.1.100",
        username="admin",
        password="password",
        transport=httpx.MockTransport(handler),
    )

    with client:
        assert login_called
        assert client._sid == "test-sid-123"

    assert logout_called


def test_synology_client_login_failure_raises() -> None:
    """Test that failed login raises an error."""
    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        return httpx.Response(200, json={"success": False, "error": {"code": 400}})

    client = SynologyClient(
        base_url="https://192.168.1.100",
        username="admin",
        password="wrong",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(RuntimeError, match="Synology login failed"):
        client.login()


def test_synology_backup_tasks_fallback_api() -> None:
    """Test that backup tasks falls back to SYNO.Core.Backup if SYNO.Backup.Task fails."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        calls.append(params)

        if params.get("api") == "SYNO.API.Auth" and params.get("method") == "login":
            return httpx.Response(200, json=_SID_RESPONSE)

        # First API fails
        if params.get("api") == "SYNO.Backup.Task" and params.get("method") == "list":
            return httpx.Response(500, json={"success": False})

        # Fallback succeeds
        if params.get("api") == "SYNO.Core.Backup" and params.get("method") == "list":
            return httpx.Response(200, json=_BACKUP_TASKS_RESPONSE)

        if params.get("api") == "SYNO.API.Auth" and params.get("method") == "logout":
            return httpx.Response(200, json={"success": True})

        return httpx.Response(404, json={"error": "Not found"})

    client = SynologyClient(
        base_url="https://192.168.1.100",
        username="admin",
        password="password",
        transport=httpx.MockTransport(handler),
    )

    with client:
        tasks = client.get_backup_tasks()

    assert len(tasks) == 2
    assert tasks[0]["name"] == "Photos Backup"
    # Verify both APIs were tried
    apis_tried = [c["api"] for c in calls if "api" in c]
    assert "SYNO.Backup.Task" in apis_tried
    assert "SYNO.Core.Backup" in apis_tried


def test_synology_tool_permission_is_read() -> None:
    """Test that the tool has READ permission (no side effects)."""
    from app.plugins.base import ToolPermission
    client = _mock_synology_client()
    tool = SynologyGetStatusTool(client)
    assert tool.permission is ToolPermission.READ


def test_synology_tool_parameters_empty() -> None:
    """Test that the tool takes no parameters."""
    client = _mock_synology_client()
    tool = SynologyGetStatusTool(client)
    assert tool.parameters == {"type": "object", "properties": {}, "required": []}


def test_synology_tool_name_and_description() -> None:
    """Test that the tool has the correct name and description."""
    client = _mock_synology_client()
    tool = SynologyGetStatusTool(client)
    assert tool.name == "synology_get_status"
    assert "Synology NAS" in tool.description
    assert "disk SMART" in tool.description
    assert "Hyper Backup" in tool.description