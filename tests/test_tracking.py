"""5.51: stop tracking something removed on purpose (Open WebUI kept being "down")."""

from fastapi.testclient import TestClient

from app.homelab import tracking
from app.remediation import runner
from tests.test_remediation import FakeDocker
from tests.test_remediation import _siris as _docker_siris


def _siris(docker, **kw):
    from app.plugins.builtin.tracking import ListUntrackedTool, ResumeTrackingTool, StopTrackingTool

    siris, pushes = _docker_siris(docker, **kw)
    for tool in (StopTrackingTool(siris), ResumeTrackingTool(siris), ListUntrackedTool(siris)):
        if siris.tools.get(tool.name) is None:
            siris.tools.register(tool)
    return siris, pushes


def _reset(siris) -> None:
    siris.state.delete(tracking.KEY)
    siris.state.delete(runner.LEDGER_KEY)


def test_an_untracked_container_is_not_checked_restarted_or_pushed(client: TestClient) -> None:
    docker = FakeDocker({"open-webui": {"State": "exited", "Status": "Exited (0) 2 days ago"}, "plex": {"State": "running", "Status": "Up"}}, recovers=set())
    siris, pushes = _siris(docker, watched="*")
    _reset(siris)
    warnings = siris.run_tool("server_health_check", {})["warnings"]
    assert any("open-webui" in w and 'stop tracking open-webui' in w for w in warnings)

    said = siris.run_tool("stop_tracking", {"name": "Open WebUI", "why": "removed it"})
    assert said["name"] == "open-webui" and said["tracked"] is False  # the container's own name, from what was said
    assert not any("open-webui" in w for w in siris.run_tool("server_health_check", {})["warnings"])
    assert runner.run(siris)["handled"] == [] and docker.restarts == [] and pushes.sent == []
    assert siris.run_tool("list_untracked", {})["untracked"][0]["why"] == "removed it"

    siris.run_tool("resume_tracking", {"name": "open-webui"})
    assert any("open-webui" in w for w in siris.run_tool("server_health_check", {})["warnings"])
    _reset(siris)


def test_the_self_healing_alert_says_how_to_stop_tracking(client: TestClient) -> None:
    docker = FakeDocker({"open-webui": {"State": "exited", "Status": "Exited (1)"}}, recovers=set())
    siris, pushes = _siris(docker, watched="open-webui")
    _reset(siris)
    runner.run(siris)
    assert 'Say "stop tracking open-webui"' in pushes.sent[0]["message"]
    _reset(siris)


def test_tracking_over_the_hub(client: TestClient) -> None:
    from app.core.siris import get_siris
    from tests.test_hub import app

    docker = FakeDocker({"open-webui": {"State": "exited", "Status": "Exited (0)"}, "plex": {"State": "running", "Status": "Up"}}, recovers=set())
    siris, _ = _siris(docker)
    _reset(siris)
    app.dependency_overrides[get_siris] = lambda: siris
    try:
        listed = client.get("/siris/hub/v1/tracking").json()
        assert [(c["name"], c["tracked"]) for c in listed["containers"]] == [("open-webui", True), ("plex", True)]  # stopped first
        off = client.put("/siris/hub/v1/tracking", json={"kind": "container", "name": "open-webui", "tracked": False, "why": "removed"}).json()
        assert [(c["name"], c["tracked"]) for c in off["containers"]] == [("plex", True), ("open-webui", False)]
        assert off["untracked"][0]["name"] == "open-webui"
        back = client.put("/siris/hub/v1/tracking", json={"kind": "container", "name": "open-webui", "tracked": True}).json()
        assert back["untracked"] == []
        assert client.put("/siris/hub/v1/tracking", json={"kind": "device", "name": "x", "tracked": False}).status_code == 422
    finally:
        app.dependency_overrides.pop(get_siris, None)
        _reset(siris)
