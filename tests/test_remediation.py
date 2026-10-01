"""Self-healing (app/remediation/runner.py): restart watched containers that
go down, within a budget, and escalate with logs when that doesn't work."""

import struct
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.siris import SirisAI
from app.docker.client import DockerClient
from app.remediation import runner


class FakeDocker:
    def __init__(self, containers: dict[str, dict[str, str]], recovers: set[str]) -> None:
        self.containers = containers
        self.recovers = recovers
        self.restarts: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/containers/json":
            return httpx.Response(200, json=[{"Id": f"{name}000000000000", "Names": [f"/{name}"], "Image": "img", **c} for name, c in self.containers.items()])
        if path.endswith("/restart"):
            name = path.split("/")[2]
            self.restarts.append(name)
            if name in self.recovers:
                self.containers[name] = {"State": "running", "Status": "Up 1 second"}
            return httpx.Response(204)
        if path.endswith("/logs"):
            text = b"2026-10-01T01:00:00Z panic: database is locked\n2026-10-01T01:00:01Z exiting\n"
            return httpx.Response(200, content=struct.pack(">BxxxL", 2, len(text)) + text)
        return httpx.Response(404)


class _Pushes:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    def send(self, message: str, title: str | None = None, actions: Any = None, priority: str | None = None) -> None:
        self.sent.append({"message": message, "title": title, "priority": priority})

    def close(self) -> None:
        pass


def _siris(docker: FakeDocker, watched: str = "plex,sonarr", **settings: Any) -> tuple[SirisAI, _Pushes]:
    siris = SirisAI(settings=Settings(remediation_containers=watched, ntfy_topic="t", remediation_settle_seconds=0, **settings))
    client = DockerClient(base_url="", transport=httpx.MockTransport(docker.handler))
    from app.plugins.builtin.docker import DockerGetContainerLogsTool, DockerListContainersTool, DockerResolveContainerTool, DockerRestartContainerTool
    from app.plugins.builtin.remediation import RemediationStatusTool

    siris._docker_client = client
    for tool in (DockerListContainersTool(client), DockerResolveContainerTool(client), DockerGetContainerLogsTool(client), DockerRestartContainerTool(client, siris.entities), RemediationStatusTool(siris)):
        if siris.tools.get(tool.name) is None:
            siris.tools.register(tool)
    pushes = _Pushes()
    siris.tools.get("notify")._client = pushes  # type: ignore[union-attr]
    return siris, pushes


@pytest.fixture(autouse=True)
def _reset(client: TestClient) -> None:
    SirisAI(settings=Settings()).state.delete(runner.LEDGER_KEY)


def test_unhealthy_reason() -> None:
    assert runner.unhealthy_reason({"state": "running", "status": "Up 2 hours"}) is None
    assert "exited" in runner.unhealthy_reason({"state": "exited", "status": "Exited (1) 2 minutes ago"})
    assert runner.unhealthy_reason({"state": "running", "status": "Up 5 minutes (unhealthy)"}) == "its health check is failing"
    assert runner.unhealthy_reason({"state": "restarting", "status": "Restarting (1)"}) == "it is stuck restarting"


def test_a_watched_container_that_recovers_is_restarted_and_noted_for_the_briefing() -> None:
    docker = FakeDocker({"plex": {"State": "exited", "Status": "Exited (137)"}, "other": {"State": "exited", "Status": "Exited (1)"}}, recovers={"plex"})
    siris, pushes = _siris(docker)

    result = runner.run(siris)
    assert docker.restarts == ["plex"]  # 'other' isn't watched
    assert result["handled"][0]["fixed"] is True
    assert pushes.sent == []
    assert any("plex was down" in n["message"] for n in siris.pending_notifications.list_pending())
    audit = siris.audit.find_recent("docker_restart_container", argument_contains="plex", limit=1)[0]
    assert audit["trigger"].startswith("self-healing")


def test_a_container_that_is_briefly_up_after_restart_then_crashes_is_not_fixed() -> None:
    """Found live: docker_restart_container's own verify() saw 'running' for a
    container that crashed a moment later."""

    class CrashesAfterStart(FakeDocker):
        def handler(self, request: httpx.Request) -> httpx.Response:
            response = super().handler(request)
            if request.url.path.endswith("/restart"):
                self.containers["sonarr"] = {"State": "exited", "Status": "Exited (1) 1 second ago"}
            return response

    docker = CrashesAfterStart({"sonarr": {"State": "exited", "Status": "Exited (1)"}}, recovers={"sonarr"})
    siris, pushes = _siris(docker)
    outcome = runner.run(siris)["handled"][0]
    assert outcome["fixed"] is False and "exited" in outcome["detail"]
    assert pushes.sent and pushes.sent[0]["priority"] == "urgent"


def test_a_crash_looping_container_gets_limited_restarts_then_one_alert_with_logs() -> None:
    docker = FakeDocker({"sonarr": {"State": "exited", "Status": "Exited (1)"}}, recovers=set())
    siris, pushes = _siris(docker)
    start = datetime(2026, 10, 1, 1, 0, tzinfo=UTC)

    first = runner.run(siris, now=start)["handled"][0]
    assert first["action"] == "restarted" and first["fixed"] is False
    assert len(pushes.sent) == 1 and pushes.sent[0]["priority"] == "urgent"
    assert "database is locked" in pushes.sent[0]["message"]

    runner.run(siris, now=start + timedelta(minutes=5))  # second restart, already alerted this hour
    third = runner.run(siris, now=start + timedelta(minutes=10))["handled"][0]
    assert docker.restarts == ["sonarr", "sonarr"]  # capped at 2 per container per hour
    assert third["action"] == "none" and "already restarted 2" in third["detail"]
    assert len(pushes.sent) == 1

    # An hour later the budget resets and it tries again.
    runner.run(siris, now=start + timedelta(minutes=75))
    assert docker.restarts == ["sonarr", "sonarr", "sonarr"]


def test_the_hourly_budget_covers_all_containers() -> None:
    docker = FakeDocker({f"c{i}": {"State": "exited", "Status": "Exited (1)"} for i in range(5)}, recovers=set())
    siris, _ = _siris(docker, watched="*", remediation_max_actions_per_hour=3)
    runner.run(siris)
    assert len(docker.restarts) == 3
    status = siris.run_tool("remediation_status", {})
    assert status["budget_left_this_hour"] == 0 and len(status["actions_this_hour"]) == 3


def test_nothing_happens_without_watched_containers() -> None:
    docker = FakeDocker({"plex": {"State": "exited", "Status": "Exited (1)"}}, recovers={"plex"})
    siris, _ = _siris(docker, watched="")
    assert runner.run(siris) == {"watched": [], "handled": []}
    assert docker.restarts == []
