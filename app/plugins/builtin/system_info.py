"""System resource info -- CPU, memory, disk, temperatures (where
available) on the machine SirisAI is running on.

Backed by `psutil` reading local /proc, not an external service, so unlike
the other tools in this file's package there's no base_url/token to
configure and it's always registered. Note this reflects the *container's*
view unless deployed with the host's PID namespace and relevant mounts --
see the README for the docker-compose tweak that gives it a real host view.
"""

from typing import Any

import psutil

from app.plugins.base import Tool, ToolPermission


class SystemInfoTool(Tool):
    name = "system_info"
    description = (
        "Get current system resource usage: CPU load, memory, disk usage, and "
        "temperatures (where available) on the machine SirisAI is running on."
    )
    permission = ToolPermission.READ
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, disk_paths: list[str]) -> None:
        self._disk_paths = disk_paths

    def run(self, **kwargs: Any) -> Any:
        memory = psutil.virtual_memory()
        disk = {}
        for path in self._disk_paths:
            try:
                usage = psutil.disk_usage(path)
            except OSError:
                continue
            disk[path] = {
                "total_gb": round(usage.total / 1e9, 1),
                "used_gb": round(usage.used / 1e9, 1),
                "percent_used": usage.percent,
            }

        temperatures: dict[str, float] = {}
        try:
            for label, entries in (psutil.sensors_temperatures() or {}).items():
                for entry in entries:
                    key = f"{label}:{entry.label}" if entry.label else label
                    temperatures[key] = entry.current
        except (AttributeError, OSError):
            pass  # sensors_temperatures() is Linux-only and not always available.

        return {
            "cpu_percent": psutil.cpu_percent(interval=0.2),
            "cpu_count": psutil.cpu_count(),
            "memory": {
                "total_gb": round(memory.total / 1e9, 1),
                "used_gb": round(memory.used / 1e9, 1),
                "percent_used": memory.percent,
            },
            "disk": disk,
            "temperatures_celsius": temperatures,
        }
