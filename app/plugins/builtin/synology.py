"""Synology NAS status tool -- disk health, storage usage, backup task status.

Backed by Synology DSM REST API (Web API) via SynologyClient, authenticated
via SYNO.API.Auth. Requires SIRISAI_SYNOLOGY_BASE_URL, SIRISAI_SYNOLOGY_USERNAME,
SIRISAI_SYNOLOGY_PASSWORD in .env to be registered.

SYNO.Core.System -> system status (model, serial, temperature, fan status)
SYNO.Core.Storage -> storage/volume info (volumes, disks, usage)
SYNO.Core.Storage disk_smart_info_get -> SMART health per disk
SYNO.Backup.Task / SYNO.Core.Backup -> Hyper Backup task status
"""

from typing import Any

from app.plugins.base import Tool, ToolPermission
from app.synology.client import SynologyClient


class SynologyGetStatusTool(Tool):
    name = "synology_get_status"
    description = (
        "Get Synology NAS health and status: system info, storage/volume usage, "
        "disk SMART health, and Hyper Backup task status."
    )
    permission = ToolPermission.READ
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, client: SynologyClient) -> None:
        self._client = client

    def run(self, **kwargs: Any) -> Any:
        # Use the client as a context manager to handle login/logout
        with self._client:
            system_status = self._client.get_system_status()
            storage_info = self._client.get_storage_info()
            disk_smart = self._client.get_disk_smart_info()
            backup_tasks = self._client.get_backup_tasks()

        return {
            "system": system_status,
            "storage": storage_info,
            "disk_smart": disk_smart,
            "backup_tasks": backup_tasks,
        }