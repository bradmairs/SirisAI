"""Reference skill: a server health check composed from existing tools.
Copy this file's shape for the next skill.

Time-of-day anomaly detection (a Jarvis-concept brainstorm item):
_check_metric() prefers MetricsStore.hourly_baseline() for the current
hour over the flat baseline() when the hourly one has enough history --
a value that's unremarkable against a server's all-day average can still
be genuinely unusual for 3am specifically (see app/metrics/service.py's
module docstring). Falls back to the flat baseline, then the fixed
threshold, in the same order baseline() itself already falls back to the
fixed threshold -- one more rung on the same ladder, not a replacement
for it.
"""

from datetime import UTC, datetime
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
# Lower than _MIN_SAMPLES_FOR_BASELINE on purpose: an hourly bucket
# accumulates roughly 1/24th as fast as the flat baseline does over the
# same wall-clock time, so requiring the same count would mean it
# essentially never activates on a real install.
_MIN_SAMPLES_FOR_HOURLY_BASELINE = 5


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
        docker_note = None
        if siris.tools.get("docker_list_containers") is not None:
            # Docker being unreachable (socket not mounted, daemon down) is a
            # fact about the check, not a reason to fail the whole health
            # check -- found live: it took server_health_check, daily_briefing
            # and every "good morning" down with it.
            try:
                containers = siris.run_tool("docker_list_containers", {"all_containers": True})
            except Exception as exc:
                docker_note = f"Docker status unavailable: {exc}"
            else:
                for container in containers:
                    if container["state"] != "running":
                        label = container["names"][0] if container["names"] else container["id"]
                        warnings.append(f"Container '{label}' is {container['state']}")

        # Homelab power draw (milestone 47's cost/resource dashboard) --
        # this codebase has no power sensor of its own, so it reuses the
        # existing Home Assistant integration: whatever entity
        # SIRISAI_POWER_SENSOR_ENTITY_ID names is read as a wattage
        # reading every time a health check runs, same accumulation
        # pattern as cpu/memory/disk above. Best-effort, same reasoning
        # as milestone 39's weather/calendar wrapping -- an unreachable
        # Home Assistant or a non-numeric sensor state doesn't fail the
        # health check, it just means no sample this time.
        if siris.settings.power_sensor_entity_id and siris.tools.get("home_assistant_get_state") is not None:
            try:
                state = siris.run_tool(
                    "home_assistant_get_state", {"entity_id": siris.settings.power_sensor_entity_id}
                )
                siris.metrics.record_sample("power_draw_watts", float(state["state"]))
            except Exception:
                pass

        return {
            "system": system,
            "docker_containers": containers,
            "docker_note": docker_note,
            "warnings": warnings,
            "healthy": not warnings,
        }

    def _check_metric(self, siris: "SirisAI", warnings: list[str], metric_name: str, value: float, label: str) -> None:
        """Records `value` as a new sample (building history for future
        calls) and flags it if it's above whichever is lower: the fixed
        threshold, or (once enough history exists) this server's own
        mean + N*stdev -- preferring the current hour's own baseline
        over the flat one when it has enough samples, since a value can
        be unremarkable across a full day/night cycle but genuinely
        unusual for this specific hour (see app/metrics/service.py's
        module docstring). Message always starts "{label} is high:
        {value}%" regardless of which threshold triggered it --
        NotificationManager (26) pattern-matches on that exact
        prefix/shape, so a personalized flag still correlates the same
        way a fixed-threshold one does; the baseline detail is appended
        after, not instead."""
        siris.metrics.record_sample(metric_name, value)
        current_hour = datetime.now(UTC).hour
        hourly_baseline = siris.metrics.hourly_baseline(
            metric_name, current_hour, min_samples=_MIN_SAMPLES_FOR_HOURLY_BASELINE
        )
        baseline = hourly_baseline or siris.metrics.baseline(
            metric_name, min_samples=_MIN_SAMPLES_FOR_BASELINE
        )

        if baseline is not None:
            threshold = min(_HIGH_USAGE_THRESHOLD_PERCENT, baseline.mean + _BASELINE_STDEV_MULTIPLIER * baseline.stdev)
        else:
            threshold = _HIGH_USAGE_THRESHOLD_PERCENT

        if value <= threshold:
            return

        message = f"{label} is high: {value}%"
        if baseline is not None:
            kind = "hourly" if baseline is hourly_baseline else "overall"
            message += f" ({kind} baseline {baseline.mean:.1f}% ± {baseline.stdev:.1f}%, {baseline.sample_count} samples)"
        warnings.append(message)
