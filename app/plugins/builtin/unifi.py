"""UniFi tools -- device and client visibility, via app/unifi/client.py.
Registered only when SIRISAI_UNIFI_BASE_URL and SIRISAI_UNIFI_API_KEY are
both set. Generate an API key from the controller's own local UI (its
LAN IP -> Settings -> Control Plane -> Integrations) -- see
app/unifi/client.py for why that's a different key/product than UniFi's
cloud account API.

List tools are READ. block_client/unblock_client (a later milestone,
extending this integration past its milestone-57 read-only-by-design
stance at explicit user request) are CONFIRM_REQUIRED -- blocking
disconnects a device and keeps it off the network until unblocked, a
real, immediate effect on whoever's using it, the same "don't run this
without a human approving it first" tier as home_assistant_call_service
locking a door or Radarr/Sonarr deleting a library item. Deliberately
scoped to clients only, not devices: restarting a UniFi device (AP,
switch, gateway) risks taking down the LAN segment this very SirisAI
instance may be running on, a materially larger blast radius than
disconnecting one client -- left for a future milestone that can weigh
that risk on its own, not folded in here.
"""

from typing import Any

from app.plugins.base import Tool, ToolPermission, ToolVerification
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


class UniFiBlockClientTool(Tool):
    name = "unifi_block_client"
    description = (
        "Block a device from the UniFi network, using its id from unifi_list_clients. "
        "Immediately disconnects it and prevents it reconnecting until unblocked."
    )
    permission = ToolPermission.CONFIRM_REQUIRED
    parameters = {
        "type": "object",
        "properties": {
            "client_id": {
                "type": "string",
                "description": "The UniFi client id to block, from unifi_list_clients.",
            },
        },
        "required": ["client_id"],
    }

    def __init__(self, client: UniFiClient) -> None:
        self._client = client

    def run(self, client_id: str = "", **kwargs: Any) -> Any:
        return self._client.block_client(client_id)

    def verify(self, arguments: dict[str, Any], result: Any) -> ToolVerification:
        """Same "the API call not raising doesn't prove it happened" gap
        this codebase's other verify()s close -- re-lists clients and
        checks the `blocked` flag on this one. If the client no longer
        appears in the list at all, that's genuinely ambiguous rather
        than a clear success or failure: it's consistent with having
        been disconnected by the block, but could just as easily mean
        it was already offline before this call and the "clients"
        endpoint only ever lists currently-connected devices -- there's
        no real controller available to confirm which (see
        app/unifi/client.py's module docstring), so this honestly raises
        NotImplementedError for that case rather than guessing either
        way."""
        client_id = arguments.get("client_id")
        if not client_id:
            raise NotImplementedError(
                f"{self.name} does not support verify without a client_id"
            )

        match = next(
            (c for c in self._client.list_clients() if c.get("id") == client_id), None
        )
        if match is None:
            raise NotImplementedError(
                f"{self.name} cannot verify: client {client_id} no longer appears in "
                "unifi_list_clients, which is ambiguous (could mean blocked-and-"
                "disconnected, or just already offline) without a real controller to "
                "confirm against"
            )
        if match.get("blocked") is True:
            return ToolVerification(
                verified=True, detail=f"Client {client_id} confirmed blocked."
            )
        return ToolVerification(
            verified=False, detail=f"Client {client_id} does not appear blocked."
        )


class UniFiUnblockClientTool(Tool):
    name = "unifi_unblock_client"
    description = (
        "Unblock a previously-blocked device on the UniFi network, using its id from "
        "unifi_list_clients, restoring its ability to connect."
    )
    permission = ToolPermission.CONFIRM_REQUIRED
    parameters = {
        "type": "object",
        "properties": {
            "client_id": {
                "type": "string",
                "description": "The UniFi client id to unblock, from unifi_list_clients.",
            },
        },
        "required": ["client_id"],
    }

    def __init__(self, client: UniFiClient) -> None:
        self._client = client

    def run(self, client_id: str = "", **kwargs: Any) -> Any:
        return self._client.unblock_client(client_id)

    def verify(self, arguments: dict[str, Any], result: Any) -> ToolVerification:
        """Mirror of UniFiBlockClientTool.verify() -- unlike blocking, an
        unblocked client not appearing in the list is unambiguous: it
        just means nothing has reconnected yet, which doesn't
        contradict a successful unblock, so there's still nothing to
        confirm from that case. Only a client found with `blocked` still
        true is a clear failure signal."""
        client_id = arguments.get("client_id")
        if not client_id:
            raise NotImplementedError(
                f"{self.name} does not support verify without a client_id"
            )

        match = next(
            (c for c in self._client.list_clients() if c.get("id") == client_id), None
        )
        if match is None or match.get("blocked") is not True:
            return ToolVerification(
                verified=True, detail=f"Client {client_id} confirmed not blocked."
            )
        return ToolVerification(
            verified=False, detail=f"Client {client_id} still appears blocked."
        )
