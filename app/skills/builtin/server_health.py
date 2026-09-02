"""Reference skill: a server health check composed from existing tools.
Copy this file's shape for the next skill.
"""

from typing import TYPE_CHECKING, Any

from app.skills.base import Skill

if TYPE_CHECKING:
    from app.core.siris import SirisAI

_HIGH_USAGE_THRESHOLD_PERCENT = 90
# Personalized thresholds (milestone 27): once a metric has enough
# history, flag it as unusual for THIS server at mean + N stdev instead
# of the fixed threshold -- but never above the fixed threshold, which
# stays a hard safety ceiling regardless of how "normal" running hot is
# for this particular server.
_BASELINE_STDEV_MULTIPLIER = 2
_MIN_SAMPLES_FOR_BASELINE = 10


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
        self._check_metric(siris, warnings, "cpu_percent", system["cpu_percent"], "CPU usage")
        self._check_metric(siris, warnings, "memory_percent", system["memory"]["percent_used"], "Memory usage")
        for path, usage in system["disk"].items():
            self._check_metric(siris, warnings, f"disk_percent:{path}", usage["percent_used"], f"Disk usage on {path}")

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

    def _check_metric(self, siris: "SirisAI", warnings: list[str], metric_name: str, value: float, label: str) -> None:
        """Records `value` as a new sample (building history for future
        calls) and flags it if it's above whichever is lower: the fixed
        threshold, or (once enough history exists) this server's own
        mean + N*stdev. Message always starts "{label} is high: {value}%"
        regardless of which threshold triggered it -- NotificationManager
        (26) pattern-matches on that exact prefix/shape, so a personalized
        flag still correlates the same way a fixed-threshold one does;
        the baseline detail is appended after, not instead."""
        siris.metrics.record_sample(metric_name, value)
        baseline = siris.metrics.baseline(metric_name, min_samples=_MIN_SAMPLES_FOR_BASELINE)

        if baseline is not None:
            threshold = min(_HIGH_USAGE_THRESHOLD_PERCENT, baseline.mean + _BASELINE_STDEV_MULTIPLIER * baseline.stdev)
        else:
            threshold = _HIGH_USAGE_THRESHOLD_PERCENT

        if value <= threshold:
            return

        message = f"{label} is high: {value}%"
        if baseline is not None:
            message += f" (baseline {baseline.mean:.1f}% ± {baseline.stdev:.1f}%, {baseline.sample_count} samples)"
        warnings.append(message)
