"""UniFi tools -- device and client visibility, via app/unifi/client.py.
Registered only when SIRISAI_UNIFI_BASE_URL and SIRISAI_UNIFI_API_KEY are
both set. Generate an API key from the controller's own local UI (its
LAN IP -> Settings -> Control Plane -> Integrations) -- see
app/unifi/client.py for why that's a different key/product than UniFi's
cloud account API.

List tools are READ. resolve_client (a later milestone) is the same
ambiguity-resolution gate milestone 66 built for
home_assistant_resolve_entity/docker_resolve_container, applied here:
block/unblock_client need an exact client_id, which nothing before this
tool could turn a loose description ("the kid's phone") into without
the model guessing at unifi_list_clients output itself.

block_client/unblock_client (a later milestone,
extending this integration past its milestone-57 read-only-by-design
stance at explicit user request) are CONFIRM_REQUIRED -- blocking
disconnects a device and keeps it off the network until unblocked, a
real, immediate effect on whoever's using it, the same "don't run this
without a human approving it first" tier as home_assistant_call_service
locking a door or Radarr/Sonarr deleting a library item.
reconnect_client (a direct follow-up, offered as the lower-risk
alternative to a UniFi *device* restart) is the same CONFIRM_REQUIRED
tier despite a smaller blast radius than block -- it never leaves a
client unable to reconnect, just cycles it, for troubleshooting rather
than restricting access.

restart_device (a further milestone) is the highest-consequence tool in
this file: restarting a UniFi device (AP, switch, gateway) risks taking
down the LAN segment this very SirisAI instance may be running on --
raised explicitly, twice, before being built, and built only at
explicit user request both times. CONFIRM_REQUIRED like every other
write here, plus a `dry_run()` that flags when the target device's
model name looks like a gateway (the single-point-of-failure case,
worse than an AP/switch) so whoever confirms sees that warning before
approving, not after. Deliberately no `verify()`: a full device reboot
takes on the order of a minute or more, far longer than any single
post-call check could usefully wait for, so checking immediately would
report "not verified" on essentially every successful restart -- worse
than not checking at all.
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


class UniFiResolveClientTool(Tool):
    name = "unifi_resolve_client"
    description = (
        "Find UniFi clients matching a loose name or description (e.g. 'kid's phone' or "
        "'kitchen tv'), checking both name and IP address. Returns every match, not just the "
        "best one -- use this before calling unifi_block_client/unifi_unblock_client on "
        "something referred to by description rather than an exact client_id, and if "
        "`ambiguous` comes back true, ask which one was meant rather than guessing."
    )
    permission = ToolPermission.READ
    parameters = {
        "type": "object",
        "properties": {
            "name_hint": {
                "type": "string",
                "description": "Loose name or description to match, e.g. 'kid's phone'.",
            },
        },
        "required": ["name_hint"],
    }

    def __init__(self, client: UniFiClient) -> None:
        self._client = client

    def run(self, name_hint: str = "", **kwargs: Any) -> Any:
        needle_words = name_hint.lower().split()
        matches = [
            client
            for client in self._client.list_clients()
            if all(
                word
                in f"{client.get('name') or ''} {client.get('ip_address') or ''}".lower()
                for word in needle_words
            )
        ]
        return {"matches": matches, "ambiguous": len(matches) > 1}


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


class UniFiReconnectClientTool(Tool):
    name = "unifi_reconnect_client"
    description = (
        "Force a device to disconnect and immediately re-associate with the UniFi network, "
        "using its id from unifi_list_clients -- for troubleshooting a flaky connection, not "
        "restricting access. Unlike unifi_block_client, the device is never left unable to "
        "reconnect."
    )
    permission = ToolPermission.CONFIRM_REQUIRED
    parameters = {
        "type": "object",
        "properties": {
            "client_id": {
                "type": "string",
                "description": "The UniFi client id to reconnect, from unifi_list_clients.",
            },
        },
        "required": ["client_id"],
    }

    def __init__(self, client: UniFiClient) -> None:
        self._client = client

    def run(self, client_id: str = "", **kwargs: Any) -> Any:
        return self._client.reconnect_client(client_id)

    # No verify(): unlike block/unblock, a forced reconnect has no
    # persistent flag to check afterward -- the client is expected to
    # briefly drop and then re-associate on its own timing, the same
    # "no single correct end state within this call" gap
    # HomeAssistantCallServiceTool.verify() already raises
    # NotImplementedError for (a scene activation, a non-boolean
    # set_temperature). Honestly nothing to check here either, so this
    # relies on Tool.verify()'s own base "doesn't support verify"
    # default rather than adding one that can't mean anything.


# Model name prefixes that indicate a UniFi gateway/router (the site's
# single point of internet/LAN routing) rather than an AP or switch --
# UDM (Dream Machine family), UDR (Dream Router), UXG (the standalone
# "Next-Gen Gateway" line), USG (the older UniFi Security Gateway).
# Best-effort, not authoritative: an unrecognized model name just means
# no warning fires, same "don't overclaim rigor that isn't there"
# honesty as DockerRestartContainerTool.dry_run()'s own best-effort
# Plex-session detail.
_LIKELY_GATEWAY_MODEL_PREFIXES = ("UDM", "UDR", "UXG", "USG")


class UniFiRestartDeviceTool(Tool):
    name = "unifi_restart_device"
    description = (
        "Restart a UniFi network device (access point, switch, or gateway), using its id "
        "from unifi_list_devices. Disrupts every client connected through that device for "
        "the length of a full reboot (a minute or more) -- restarting the site's gateway "
        "disrupts ALL internet/LAN routing for the whole site, not just one device's "
        "clients, possibly including SirisAI's own connectivity. Call unifi_restart_device "
        "with dry_run to see the target device's current state and whether it looks like a "
        "gateway before confirming."
    )
    permission = ToolPermission.CONFIRM_REQUIRED
    parameters = {
        "type": "object",
        "properties": {
            "device_id": {
                "type": "string",
                "description": "The UniFi device id to restart, from unifi_list_devices.",
            },
        },
        "required": ["device_id"],
    }

    def __init__(self, client: UniFiClient) -> None:
        self._client = client

    def run(self, device_id: str = "", **kwargs: Any) -> Any:
        return self._client.restart_device(device_id)

    def dry_run(self, device_id: str = "", **kwargs: Any) -> Any:
        """What restarting `device_id` would do, without doing it: its
        current name/model/state, and a prominent warning when the
        model name matches the gateway prefixes above -- the
        single-point-of-failure case a human approving this should see
        before confirming, not discover after. Same shape/intent as
        DockerRestartContainerTool.dry_run() (milestone 25's preview
        mechanism)."""
        devices = self._client.list_devices()
        match = next((d for d in devices if d.get("id") == device_id), None)

        report: dict[str, Any] = {
            "device_id": device_id,
            "name": match["name"] if match else None,
            "model": match["model"] if match else None,
            "current_state": match["state"] if match else "unknown (not found)",
            "would_restart": True,
        }

        model = (match or {}).get("model") or ""
        if any(
            model.upper().startswith(prefix)
            for prefix in _LIKELY_GATEWAY_MODEL_PREFIXES
        ):
            report["warning"] = (
                f"Model '{model}' matches known UniFi gateway/router naming -- restarting "
                "this device will likely disrupt ALL internet/LAN routing for the site "
                "(possibly including SirisAI's own connectivity) for the length of the "
                "reboot, not just this one device's own clients."
            )

        return report

    # No verify(): a full device reboot takes on the order of a minute
    # or more (a real hardware/firmware boot cycle, not a Docker
    # container's few-second process restart) -- checking the device's
    # state immediately after this call returns would see it still
    # rebooting essentially every time, reporting "not verified" on
    # every successful restart. That's worse than not checking at all;
    # see this file's module docstring.
