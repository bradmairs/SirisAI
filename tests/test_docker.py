import httpx

from app.docker.client import DockerClient
from app.plugins.builtin.docker import DockerListContainersTool, DockerRestartContainerTool


def _mock_client(handler) -> DockerClient:
    return DockerClient(base_url="unix:///var/run/docker.sock", transport=httpx.MockTransport(handler))


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
    result = DockerRestartContainerTool(client).run(container="plex")
    assert result == {"container": "plex", "restarted": True}
    client.close()
