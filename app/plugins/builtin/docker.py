"""Docker tools -- container status and restart, via the Docker Engine
API (app/docker/client.py). Registered only when SIRISAI_DOCKER_BASE_URL
is set: point it at a Unix socket (mount the host's
/var/run/docker.sock into this container) or a TCP-exposed daemon.
"""

from typing import Any

from app.docker.client import DockerClient
from app.plugins.base import Tool, ToolPermission


class DockerListContainersTool(Tool):
    name = "docker_list_containers"
    description = "List Docker containers and their status (running, exited, etc)."
    permission = ToolPermission.READ
    parameters = {
        "type": "object",
        "properties": {
            "all_containers": {
                "type": "boolean",
                "description": "Include stopped containers too. Defaults to true.",
            },
        },
        "required": [],
    }

    def __init__(self, client: DockerClient) -> None:
        self._client = client

    def run(self, all_containers: bool = True, **kwargs: Any) -> Any:
        return self._client.list_containers(all_containers=all_containers)


class DockerRestartContainerTool(Tool):
    name = "docker_restart_container"
    description = "Restart a Docker container by name or id."
    # Routine and reversible -- matches this tool library's own
    # "restarting a known service -> SAFE_ACTION" reasoning.
    permission = ToolPermission.SAFE_ACTION
    parameters = {
        "type": "object",
        "properties": {
            "container": {"type": "string", "description": "Container name or id."},
        },
        "required": ["container"],
    }

    def __init__(self, client: DockerClient) -> None:
        self._client = client

    def run(self, container: str = "", **kwargs: Any) -> Any:
        return self._client.restart_container(container)
