"""UniFi tools -- device and client visibility, via app/unifi/client.py.
Registered only when SIRISAI_UNIFI_BASE_URL and SIRISAI_UNIFI_API_KEY are
both set. Generate an API key from the controller's own local UI (its
LAN IP -> Settings -> Control Plane -> Integrations) -- see
app/unifi/client.py for why that's a different key/product than UniFi's
cloud account API.
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
