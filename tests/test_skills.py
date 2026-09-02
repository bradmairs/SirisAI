from typing import Any

from fastapi.testclient import TestClient

from app.core.siris import SirisAI, get_siris
from app.plugins.base import ToolPermission
from app.skills.builtin.server_health import ServerHealthSkill


class _FakeToolsRegistry:
    def __init__(self, has_docker: bool) -> None:
        self._has_docker = has_docker

    def get(self, name: str) -> object | None:
        return object() if name == "docker_list_containers" and self._has_docker else None


class _FakeMetrics:
    """No history -- every metric falls back to the fixed threshold, same
    as ServerHealthSkill always behaved before milestone 27."""

    def record_sample(self, metric_name: str, value: float) -> None:
        pass

    def baseline(self, metric_name: str, min_samples: int = 10, limit: int = 100) -> Any:
        return None


class _FakeSiris:
    def __init__(self, system_info: dict[str, Any], containers: list | None = None, has_docker: bool = False) -> None:
        self.tools = _FakeToolsRegistry(has_docker)
        self.metrics = _FakeMetrics()
        self._system_info = system_info
        self._containers = containers

    def run_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        if name == "system_info":
            return self._system_info
        if name == "docker_list_containers":
            return self._containers
        raise AssertionError(f"unexpected tool call: {name}")


_HEALTHY_SYSTEM = {
    "cpu_percent": 5.0,
    "cpu_count": 4,
    "memory": {"total_gb": 16.0, "used_gb": 2.0, "percent_used": 12.0},
    "disk": {"/": {"total_gb": 100.0, "used_gb": 10.0, "percent_used": 10.0}},
    "temperatures_celsius": {},
}


def test_server_health_reports_healthy_when_nothing_abnormal() -> None:
    result = ServerHealthSkill().run(_FakeSiris(_HEALTHY_SYSTEM))
    assert result["healthy"] is True
    assert result["warnings"] == []
    assert result["docker_containers"] is None


def test_server_health_flags_high_cpu_and_disk() -> None:
    system_info = {
        **_HEALTHY_SYSTEM,
        "cpu_percent": 95.0,
        "disk": {"/": {"total_gb": 100.0, "used_gb": 95.0, "percent_used": 95.0}},
    }
    result = ServerHealthSkill().run(_FakeSiris(system_info))
    assert result["healthy"] is False
    assert any("CPU" in w for w in result["warnings"])
    assert any("Disk" in w for w in result["warnings"])


def test_server_health_flags_non_running_containers() -> None:
    containers = [{"id": "abc123", "names": ["plex"], "image": "x", "state": "exited", "status": "Exited (1)"}]
    result = ServerHealthSkill().run(_FakeSiris(_HEALTHY_SYSTEM, containers=containers, has_docker=True))
    assert result["healthy"] is False
    assert any("plex" in w and "exited" in w for w in result["warnings"])


def test_skill_registered_as_a_read_tool() -> None:
    siris = SirisAI(settings=get_siris().settings)
    tool = siris.tools.get("server_health_check")
    assert tool is not None
    assert tool.permission is ToolPermission.READ
    result = tool.run()
    assert "healthy" in result


def test_run_tool_directly_executes_echo(client: TestClient) -> None:
    response = client.post("/siris/tools/echo/run", json={"arguments": {"message": "hi"}})
    assert response.status_code == 200
    body = response.json()
    assert body["tool_call"] == {"name": "echo", "arguments": {"message": "hi"}, "result": {"echo": "hi"}}
    assert body["confirmation_required"] is None


def test_run_tool_directly_unknown_tool_returns_404(client: TestClient) -> None:
    response = client.post("/siris/tools/does_not_exist/run", json={})
    assert response.status_code == 404


def test_run_tool_directly_runs_server_health_skill(client: TestClient) -> None:
    response = client.post("/siris/tools/server_health_check/run", json={})
    assert response.status_code == 200
    assert "healthy" in response.json()["tool_call"]["result"]


def test_run_tool_directly_requires_confirmation_then_confirms(client: TestClient) -> None:
    from app.plugins.base import Tool, ToolPermission

    class _ConfirmTool(Tool):
        name = "test_direct_confirm_tool"
        description = "test"
        permission = ToolPermission.CONFIRM_REQUIRED
        parameters = {"type": "object", "properties": {}, "required": []}

        def run(self, **kwargs: Any) -> Any:
            return {"done": True}

    siris = get_siris()
    if siris.tools.get(_ConfirmTool.name) is None:
        siris.tools.register(_ConfirmTool())

    pending = client.post(f"/siris/tools/{_ConfirmTool.name}/run", json={})
    assert pending.status_code == 200
    assert pending.json()["confirmation_required"] == {"name": _ConfirmTool.name, "arguments": {}}
    assert pending.json()["tool_call"] is None

    confirmed = client.post(f"/siris/tools/{_ConfirmTool.name}/run", json={"confirmed": True})
    assert confirmed.status_code == 200
    assert confirmed.json()["tool_call"]["result"] == {"done": True}
