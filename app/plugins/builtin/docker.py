"""Docker tools -- container status and restart, via the Docker Engine
API (app/docker/client.py). Registered only when SIRISAI_DOCKER_BASE_URL
is set: point it at a Unix socket (mount the host's
/var/run/docker.sock into this container) or a TCP-exposed daemon.
"""

from typing import Any

from app.docker.client import DockerClient
from app.memory.entities import EntityMemoryStore
from app.plex.client import PlexClient
from app.plugins.base import Tool, ToolPermission, ToolVerification


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


class DockerGetContainerLogsTool(Tool):
    name = "docker_get_container_logs"
    description = (
        "Fetch recent log lines (stdout+stderr, timestamped) from a Docker container -- "
        "for diagnosing why a service is failing, before deciding whether to restart it "
        "or delegate a repair to Hermes."
    )
    permission = ToolPermission.READ
    parameters = {
        "type": "object",
        "properties": {
            "container": {"type": "string", "description": "Container name or id."},
            "tail": {
                "type": "integer",
                "description": "Number of most recent log lines to fetch. Defaults to 200.",
            },
            "contains": {
                "type": "string",
                "description": (
                    "Optional case-insensitive substring -- if given, only lines containing it are "
                    "returned (e.g. \"error\"), to cut noise out of a large log instead of reading it all."
                ),
            },
        },
        "required": ["container"],
    }

    def __init__(self, client: DockerClient) -> None:
        self._client = client

    def run(self, container: str = "", tail: int = 200, contains: str | None = None, **kwargs: Any) -> Any:
        logs = self._client.get_container_logs(container, tail=tail)
        lines = logs.splitlines()
        if contains:
            needle = contains.lower()
            lines = [line for line in lines if needle in line.lower()]
        return {"container": container, "lines": lines}


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

    def __init__(self, client: DockerClient, entities: EntityMemoryStore, plex: PlexClient | None = None) -> None:
        self._client = client
        self._entities = entities
        self._plex = plex

    def run(self, container: str = "", **kwargs: Any) -> Any:
        return self._client.restart_container(container)

    def verify(self, arguments: dict[str, Any], result: Any) -> ToolVerification:
        """Deterministic per-tool verification (a hardening pass): the
        canonical example of the gap milestone 24's planner explicitly
        left open -- Docker's own `/restart` call can return 200 and the
        container can still end up not actually running (its entrypoint
        crashes immediately on start, a common real failure mode). One
        state check right after the restart call returns, not a health
        monitor: does NOT catch a slower crash-loop (fine for a few
        seconds, then dies) -- that needs polling over time, out of scope
        here, same "don't overclaim rigor that isn't there" honesty as
        the planner's own verification note."""
        container = arguments.get("container", "")
        containers = self._client.list_containers(all_containers=True)
        match = next((c for c in containers if container in c["names"] or c["id"] == container), None)

        if match is None:
            return ToolVerification(verified=False, detail=f"Container '{container}' not found after restart.")
        if match["state"] != "running":
            return ToolVerification(
                verified=False, detail=f"Container '{container}' is '{match['state']}' after restart, not running."
            )
        return ToolVerification(verified=True, detail=f"Container '{container}' is running after restart.")

    def dry_run(self, container: str = "", **kwargs: Any) -> Any:
        """What restarting `container` would do, without doing it: its
        current state (restarting a stopped container isn't the same as
        restarting a running one), and anything the world model
        (milestone 21) knows depends on it -- e.g. a `Plex DEPENDS_ON
        plex-container` relationship recorded earlier surfaces here as a
        potential impact. If `container` itself looks like Plex and a
        Plex client is configured, also reports the concrete number of
        active streams that would be interrupted -- the specific,
        actionable detail a general dependency list can't give on its
        own. Best-effort: an unreachable Plex server doesn't fail the
        dry run, it just omits that detail."""
        containers = self._client.list_containers(all_containers=True)
        match = next((c for c in containers if container in c["names"] or c["id"] == container), None)

        report: dict[str, Any] = {
            "container": container,
            "current_state": match["state"] if match else "unknown (not found)",
            "would_restart": True,
            "world_model_dependents": [
                f"{edge['subject_entity']} ({edge['relation']})"
                for edge in self._entities.relationships_for(container)["incoming"]
            ],
        }

        if self._plex is not None and "plex" in container.lower():
            try:
                sessions = self._plex.now_playing()
            except Exception:
                pass
            else:
                report["active_plex_sessions_would_be_interrupted"] = len(sessions)

        return report
