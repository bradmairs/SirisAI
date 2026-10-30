"""Homelab tools (app/homelab/): container updates, server backups, naming
network devices, the internet outage log, and expiring certificates and
domains."""

from typing import TYPE_CHECKING, Any

from app.homelab import backups, containers, devices, expiry, internet
from app.plugins.base import Tool, ToolPermission

if TYPE_CHECKING:
    from app.core.siris import SirisAI


class _SirisTool(Tool):
    def __init__(self, siris: "SirisAI") -> None:
        self._siris = siris


class DockerCheckUpdatesTool(_SirisTool):
    name = "docker_check_updates"
    description = (
        "Check which running Docker containers have a newer image available ('any container updates?', 'is Plex "
        "up to date?'). Also lists what's up to date and what was skipped, and why."
    )
    permission = ToolPermission.READ
    parameters = {"type": "object", "properties": {}, "required": []}

    def run(self, **kwargs: Any) -> Any:
        return containers.check(self._siris)


class DockerUpdateContainersTool(_SirisTool):
    name = "docker_update_containers"
    description = (
        "Update Docker containers to their newest image: pull it, recreate the container with the same settings, "
        "and roll back automatically if it doesn't come up healthy. Asks the user first. containers is a name, "
        "several separated by commas, or 'all' for every container with an update (see docker_check_updates)."
    )
    permission = ToolPermission.CONFIRM_REQUIRED
    parameters = {
        "type": "object",
        "properties": {"containers": {"type": "string", "description": "Container name(s), comma-separated, or 'all'."}},
        "required": ["containers"],
    }

    def run(self, containers: str = "", **kwargs: Any) -> Any:
        from app.homelab import containers as module

        wanted = [c.strip() for c in (containers or "").split(",") if c.strip()]
        if not wanted:
            raise ValueError("Name the container(s), or say 'all'.")
        if any(w.lower() == "all" for w in wanted):
            wanted = [u["container"] for u in module.check(self._siris, raise_feed=False)["updates"]]
            if not wanted:
                return {"status": "nothing to update"}
        results = module.update_many(self._siris, wanted)
        module.check(self._siris)  # refresh the feed item
        return results


class ServerBackupsCheckTool(_SirisTool):
    name = "server_backups_check"
    description = (
        "Check the backups on the servers (ZFS snapshots and backup folders set in SIRISAI_SERVER_BACKUPS): how old "
        "each newest snapshot or file is. For 'are my backups working', 'when did the NAS last back up'."
    )
    permission = ToolPermission.READ
    parameters = {"type": "object", "properties": {}, "required": []}

    def run(self, **kwargs: Any) -> Any:
        return backups.check(self._siris)


class ServerBackupTestTool(_SirisTool):
    name = "server_backup_test"
    description = (
        "Test that the server backups can be read back: reads one random file out of each backup's newest snapshot "
        "or folder (archives are listed or tested). Runs monthly by itself; use for 'test my backups'."
    )
    permission = ToolPermission.READ
    parameters = {"type": "object", "properties": {}, "required": []}

    def run(self, **kwargs: Any) -> Any:
        return backups.restore_test(self._siris)


class NetworkNameDeviceTool(_SirisTool):
    name = "network_name_device"
    description = (
        "Give a device on the network a name SirisAI remembers, which also marks it as known so it doesn't alert "
        "as new ('that Espressif thing is the kitchen plug'). device is its MAC, its current name or hostname, or "
        "its IP."
    )
    permission = ToolPermission.SAFE_ACTION
    parameters = {
        "type": "object",
        "properties": {
            "device": {"type": "string", "description": "MAC address, current name, hostname or IP."},
            "name": {"type": "string", "description": "The name to give it."},
        },
        "required": ["device", "name"],
    }

    def run(self, device: str = "", name: str = "", **kwargs: Any) -> Any:
        return devices.name_device(self._siris, devices.find_mac(self._siris, device), name)


class InternetOutagesTool(_SirisTool):
    name = "internet_outages"
    description = (
        "The internet outage log: how many times the connection dropped, for how long in total, the worst one, and "
        "speed test averages and dips. For 'was the internet down last night', 'how reliable has the NBN been', "
        "evidence for the ISP."
    )
    permission = ToolPermission.READ
    parameters = {
        "type": "object",
        "properties": {"days": {"type": "number", "description": "How many days back (default 7, up to 90)."}},
        "required": [],
    }

    def run(self, days: float = 7, **kwargs: Any) -> Any:
        return internet.summary(self._siris, max(0.1, min(float(days or 7), 90)))


class ExpiryCheckTool(_SirisTool):
    name = "expiry_check"
    description = (
        "Check when the TLS certificates (SIRISAI_SECURITY_CERT_HOSTS and HTTPS on the servers) and domain "
        "registrations (SIRISAI_SECURITY_DOMAINS) expire. For 'when does my domain expire', 'are my certs OK'."
    )
    permission = ToolPermission.READ
    parameters = {"type": "object", "properties": {}, "required": []}

    def run(self, **kwargs: Any) -> Any:
        return expiry.check(self._siris)
