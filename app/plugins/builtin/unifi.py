"""UniFi tools -- device and client visibility, via app/unifi/client.py.
Registered only when SIRISAI_UNIFI_BASE_URL and SIRISAI_UNIFI_API_KEY are
both set. Generate an API key at unifi.ui.com -> Settings -> API (see
app/unifi/client.py for why this targets the cloud Site Manager API
rather than a local controller). unifi_list_clients always raises there
-- kept registered rather than silently dropped, since UniFiClient is
the one place that would need to change if a future setup's key
authenticates against a local controller's Integration API instead,
which does support it.
"""

from typing import Any

from app.plugins.base import Tool, ToolPermission
from app.unifi.client import UniFiClient


class UniFiListDevicesTool(Tool):
    name = "unifi_list_devices"
    description = "List UniFi network devices (access points, switches, gateway) and their status."
    permission = ToolPermission.READ
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, client: UniFiClient) -> None:
        self._client = client

    def run(self, **kwargs: Any) -> Any:
        return self._client.list_devices()


class UniFiListClientsTool(Tool):
    name = "unifi_list_clients"
    description = "List devices currently connected to the UniFi network."
    permission = ToolPermission.READ
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, client: UniFiClient) -> None:
        self._client = client

    def run(self, **kwargs: Any) -> Any:
        return self._client.list_clients()
