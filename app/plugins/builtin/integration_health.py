"""integration_health: is each configured service actually reachable right now?"""

from typing import TYPE_CHECKING, Any

from app.core.health import check_integrations
from app.plugins.base import Tool, ToolPermission

if TYPE_CHECKING:
    from app.core.siris import SirisAI


class IntegrationHealthTool(Tool):
    name = "integration_health"
    description = (
        "Check whether each configured integration (Home Assistant, Plex, Radarr, Sonarr, UniFi, Obsidian, "
        "Ollama, Hermes, Docker, SSH hosts...) is reachable right now, and which tools are currently paused "
        "because they keep failing. Use for 'is everything working', 'why can't you reach X'."
    )
    permission = ToolPermission.READ
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, siris: "SirisAI") -> None:
        self._siris = siris

    def run(self, **kwargs: Any) -> Any:
        from app.core.health import broken_tools

        s = self._siris
        hosts = s.cron_client.hosts if s.cron_client is not None and hasattr(s.cron_client, "hosts") else []
        checks = check_integrations(s.settings, cron_hosts=hosts, second_brain=s.second_brain)
        result = {
            "integrations": checks,
            "down": [c["name"] for c in checks if not c["ok"]],
            "paused_tools": sorted(broken_tools(s)),
            "notes": dict(s.integration_notes),
        }
        apps_tool = s.tools.get("sirisos_apps_status")
        if apps_tool is not None:
            # The apps SirisOS monitors (APD PM, the Reviewer, the Archive...),
            # from SirisOS itself rather than a second round of checks.
            try:
                result["sirisos_apps"] = apps_tool.run()
            except Exception as exc:  # noqa: BLE001 - SirisOS being down is already in `down`
                result["sirisos_apps"] = {"error": str(exc)[:200]}
        return result
