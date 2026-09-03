import uuid

import httpx

from app.core.siris import SirisAI, get_siris
from app.docker.client import DockerClient
from app.memory.entities import EntityMemoryStore
from app.plex.client import PlexClient
from app.plugins.builtin.docker import DockerListContainersTool, DockerRestartContainerTool


def _mock_client(handler) -> DockerClient:
    return DockerClient(base_url="unix:///var/run/docker.sock", transport=httpx.MockTransport(handler))


def _entities() -> EntityMemoryStore:
    # Same SQLite-backed pattern as tests/test_entity_event_memory.py.
    return SirisAI(settings=get_siris().settings).entities


def test_list_containers_maps_fields() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/containers/json"
        assert request.url.params["all"] == "true"
        return httpx.Response(
            200,
            json=[
                {
                    "Id": "abcdef1234567890",
                    "Names": ["/plex"],
                    "Image": "plexinc/pms-docker",
                    "State": "running",
                    "Status": "Up 2 days",
                }
            ],
        )

    client = _mock_client(handler)
    result = DockerListContainersTool(client).run()
    assert result == [
        {
            "id": "abcdef123456",
            "names": ["plex"],
            "image": "plexinc/pms-docker",
            "state": "running",
            "status": "Up 2 days",
        }
    ]
    client.close()


def test_restart_container_posts_to_correct_path() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/containers/plex/restart"
        return httpx.Response(204)

    client = _mock_client(handler)
    result = DockerRestartContainerTool(client, _entities()).run(container="plex")
    assert result == {"container": "plex", "restarted": True}
    client.close()


def test_verify_reports_success_when_container_is_running_after_restart() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"Id": "abc123", "Names": ["/plex"], "State": "running"}])

    client = _mock_client(handler)
    verification = DockerRestartContainerTool(client, _entities()).verify({"container": "plex"}, {"restarted": True})

    assert verification.verified is True
    assert "running" in verification.detail
    client.close()


def test_verify_reports_failure_when_container_is_not_running_after_restart() -> None:
    """The exact gap this hardening pass exists for: the restart API call
    can return 200 even though the container immediately crashed back
    out -- this is the independent check that catches it."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"Id": "abc123", "Names": ["/plex"], "State": "exited"}])

    client = _mock_client(handler)
    verification = DockerRestartContainerTool(client, _entities()).verify({"container": "plex"}, {"restarted": True})

    assert verification.verified is False
    assert "exited" in verification.detail
    client.close()


def test_verify_reports_failure_when_container_is_missing_after_restart() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    client = _mock_client(handler)
    verification = DockerRestartContainerTool(client, _entities()).verify({"container": "ghost"}, {"restarted": True})

    assert verification.verified is False
    assert "not found" in verification.detail
    client.close()


def test_dry_run_reports_current_state_and_world_model_dependents() -> None:
    container_name = f"media-server-{uuid.uuid4()}"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"Id": "abc123", "Names": [f"/{container_name}"], "State": "running"}])

    client = _mock_client(handler)
    entities = _entities()
    dependent = f"Plex-{uuid.uuid4()}"
    entities.add_relationship(dependent, "RUNS_ON", container_name)

    report = DockerRestartContainerTool(client, entities).dry_run(container=container_name)

    assert report["current_state"] == "running"
    assert report["would_restart"] is True
    assert report["world_model_dependents"] == [f"{dependent} (RUNS_ON)"]
    assert "active_plex_sessions_would_be_interrupted" not in report
    client.close()


def test_dry_run_reports_unknown_state_for_a_container_docker_does_not_know_about() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    client = _mock_client(handler)
    report = DockerRestartContainerTool(client, _entities()).dry_run(container="ghost")

    assert report["current_state"] == "unknown (not found)"
    client.close()


def test_dry_run_reports_active_plex_sessions_when_container_looks_like_plex() -> None:
    def docker_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"Id": "abc123", "Names": ["/plex"], "State": "running"}])

    def plex_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "MediaContainer": {
                    "Metadata": [
                        {"title": "Movie A", "duration": 100, "viewOffset": 50, "User": {}, "Player": {}},
                        {"title": "Movie B", "duration": 100, "viewOffset": 10, "User": {}, "Player": {}},
                    ]
                }
            },
        )

    docker_client = _mock_client(docker_handler)
    plex_client = PlexClient(base_url="http://plex.local", token="tok", transport=httpx.MockTransport(plex_handler))

    report = DockerRestartContainerTool(docker_client, _entities(), plex_client).dry_run(container="plex")

    assert report["active_plex_sessions_would_be_interrupted"] == 2
    docker_client.close()
    plex_client.close()


def test_dry_run_omits_plex_sessions_for_a_non_plex_container_even_with_plex_configured() -> None:
    def docker_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"Id": "abc123", "Names": ["/sonarr"], "State": "running"}])

    def plex_handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("Plex should not be queried for a non-Plex container")

    docker_client = _mock_client(docker_handler)
    plex_client = PlexClient(base_url="http://plex.local", token="tok", transport=httpx.MockTransport(plex_handler))

    report = DockerRestartContainerTool(docker_client, _entities(), plex_client).dry_run(container="sonarr")

    assert "active_plex_sessions_would_be_interrupted" not in report
    docker_client.close()
    plex_client.close()


def test_dry_run_survives_plex_being_unreachable() -> None:
    def docker_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"Id": "abc123", "Names": ["/plex"], "State": "running"}])

    def plex_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    docker_client = _mock_client(docker_handler)
    plex_client = PlexClient(base_url="http://plex.local", token="tok", transport=httpx.MockTransport(plex_handler))

    report = DockerRestartContainerTool(docker_client, _entities(), plex_client).dry_run(container="plex")

    assert report["current_state"] == "running"
    assert "active_plex_sessions_would_be_interrupted" not in report
    docker_client.close()
    plex_client.close()
