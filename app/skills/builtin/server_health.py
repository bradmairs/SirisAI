"""Reference skill: a server health check composed from existing tools.
Copy this file's shape for the next skill.
"""

from typing import TYPE_CHECKING, Any

from app.skills.base import Skill

if TYPE_CHECKING:
    from app.core.siris import SirisAI

_HIGH_USAGE_THRESHOLD_PERCENT = 90


class ServerHealthSkill(Skill):
    name = "server_health_check"
    description = (
        "Run a full server health check: system resources (CPU, memory, disk, "
        "temperature) and Docker container status if Docker is configured. Summarizes "
        "anything that looks abnormal -- high resource usage, containers not running."
    )
    parameters = {"type": "object", "properties": {}, "required": []}

    def run(self, siris: "SirisAI", **kwargs: Any) -> Any:
        warnings: list[str] = []

        system = siris.run_tool("system_info", {})
        if system["cpu_percent"] > _HIGH_USAGE_THRESHOLD_PERCENT:
            warnings.append(f"CPU usage is high: {system['cpu_percent']}%")
        if system["memory"]["percent_used"] > _HIGH_USAGE_THRESHOLD_PERCENT:
            warnings.append(f"Memory usage is high: {system['memory']['percent_used']}%")
        for path, usage in system["disk"].items():
            if usage["percent_used"] > _HIGH_USAGE_THRESHOLD_PERCENT:
                warnings.append(f"Disk usage on {path} is high: {usage['percent_used']}%")

        containers = None
        if siris.tools.get("docker_list_containers") is not None:
            containers = siris.run_tool("docker_list_containers", {"all_containers": True})
            for container in containers:
                if container["state"] != "running":
                    label = container["names"][0] if container["names"] else container["id"]
                    warnings.append(f"Container '{label}' is {container['state']}")

        return {
            "system": system,
            "docker_containers": containers,
            "warnings": warnings,
            "healthy": not warnings,
        }
