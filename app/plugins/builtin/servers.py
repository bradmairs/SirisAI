"""Server tools beyond running commands (app/servers/): wake a server, check
its OS updates and disks, install updates, and see which permissions
SirisAI has on it."""

from typing import TYPE_CHECKING, Any

from app.plugins.base import Tool, ToolPermission
from app.servers import checks, wake, watch
from app.ssh.client import SshError

if TYPE_CHECKING:
    from app.core.siris import SirisAI


def _targets(siris: "SirisAI", servers: str, wake_only: bool = False) -> list[Any]:
    """The named servers ('all' = every SSH server). With wake_only, computers
    listed only for Wake-on-LAN (SIRISAI_SSH_WAKE_MACS) can be named too."""
    wanted = [s.strip() for s in (servers or "").split(",") if s.strip()]
    if not wanted:
        raise ValueError("Name the server(s), or say 'all'.")
    if any(w.lower() == "all" for w in wanted):
        return siris.ssh_client.hosts  # type: ignore[union-attr]
    extra = {m.name: m for m in wake.wake_only_machines(siris)} if wake_only else {}
    found: dict[str, Any] = {}
    for w in wanted:
        host = extra.get(w.lower())
        if host is None:
            try:
                host = siris.ssh_client.find(w)  # type: ignore[union-attr]
            except ValueError:
                if not extra:
                    raise
                raise ValueError(f"Unknown machine '{w}'. Servers: {', '.join(h.name for h in siris.ssh_client.hosts) or 'none'}; "  # type: ignore[union-attr]
                                 f"other computers: {', '.join(extra)}.") from None
        found[host.name] = host
    return list(found.values())


class SshWakeTool(Tool):
    name = "ssh_wake"
    _DESCRIPTION = (
        "Turn a server or computer on with Wake-on-LAN ('wake the NAS', 'turn my desktop on', 'wake all the servers'), "
        "then watch for it to come up and say so in the attention feed. Always asks the user first. servers is a name, "
        "a comma-separated list, or 'all' (all the servers). Besides the SSH servers, computers listed for Wake-on-LAN "
        "can be woken by name: " + "{wake_only}" + "."
    )
    permission = ToolPermission.CONFIRM_REQUIRED
    parameters = {
        "type": "object",
        "properties": {"servers": {"type": "string", "description": "A server name, several separated by commas, or 'all'."}},
        "required": ["servers"],
    }

    def __init__(self, siris: "SirisAI") -> None:
        self._siris = siris

    @property
    def description(self) -> str:  # type: ignore[override]
        names = ", ".join(m.name for m in wake.wake_only_machines(self._siris)) or "none set up yet"
        return self._DESCRIPTION.format(wake_only=names)

    def run(self, servers: str = "", **kwargs: Any) -> Any:
        results = []
        for host in _targets(self._siris, servers, wake_only=True):
            try:
                results.append(wake.wake(self._siris, host, wait=self._siris.settings.ssh_wake_timeout_seconds > 0))
            except (ValueError, RuntimeError) as exc:
                results.append({"server": host.name, "error": str(exc)})
        return results


class SshServerUpdatesTool(Tool):
    name = "ssh_server_updates"
    description = (
        "Check which OS updates are waiting on the servers (and how many are security updates), and whether any need "
        "a reboot. For 'are my servers up to date', 'any updates on the NAS'. Checks all of them; the results also go "
        "to the attention feed with Install and Reboot buttons."
    )
    permission = ToolPermission.READ
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, siris: "SirisAI") -> None:
        self._siris = siris

    def run(self, **kwargs: Any) -> Any:
        return watch.check_updates(self._siris)


class SshInstallUpdatesTool(Tool):
    name = "ssh_install_updates"
    description = (
        "Install every waiting OS update on a server (apt or dnf), in the background. Asks the user first. Use "
        "ssh_server_updates to see what's waiting; it says when a reboot is needed afterwards."
    )
    permission = ToolPermission.CONFIRM_REQUIRED
    parameters = {
        "type": "object",
        "properties": {"server": {"type": "string", "description": "Server name from ssh_list_servers."}},
        "required": ["server"],
    }

    def __init__(self, siris: "SirisAI") -> None:
        self._siris = siris

    def run(self, server: str = "", **kwargs: Any) -> Any:
        if not self._siris.settings.ssh_allow_changes:
            raise PermissionError("Changes over SSH are turned off (SIRISAI_SSH_ALLOW_CHANGES=false).")
        client = self._siris.ssh_client
        host = client.find(server)  # type: ignore[union-attr]
        found = checks.parse_updates(client.run(host.name, checks.updates_command(), timeout=60)["stdout"])  # type: ignore[union-attr]
        if not found["updates"]:
            return {"server": host.name, "status": "nothing to install"}
        result = client.run(host.name, checks.install_updates_command(host, found["package_manager"]), timeout=20)  # type: ignore[union-attr]
        if checks.needs_sudo(result):
            raise PermissionError(f"{host.user} on {host.name} needs passwordless sudo to install updates (ask me which permissions are missing)")
        if result["exit_code"] != 0:
            raise RuntimeError((result["stderr"] or result["stdout"]).strip()[-300:] or f"exit {result['exit_code']}")
        from app.servers import timeline

        timeline.add(self._siris, host.name, "updates", f"installing {found['updates']} update{'s' if found['updates'] != 1 else ''}")
        return {
            "server": host.name,
            "status": "installing",
            "updates": found["updates"],
            "note": "Running in the background; the next update check reports how it went and whether a reboot is needed.",
        }


class SshDiskHealthTool(Tool):
    name = "ssh_disk_health"
    description = (
        "Check the physical health of the servers' disks: SMART status and warning counters (reallocated or pending "
        "sectors, SSD wear, media errors), degraded ZFS pools and software RAID. For 'are my drives OK', 'is any disk "
        "failing'. Problems also go to the attention feed."
    )
    permission = ToolPermission.READ
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, siris: "SirisAI") -> None:
        self._siris = siris

    def run(self, **kwargs: Any) -> Any:
        return watch.check_disks(self._siris)


class SshServerPermissionsTool(Tool):
    name = "ssh_server_permissions"
    description = (
        "Check what SirisAI is allowed to do on each server through sudo (reboot, shut down, restart services, install "
        "updates, read SMART) and give the exact sudoers line to allow what's missing. For 'can you reboot the NAS', "
        "'why can't you restart services', 'what do I need to set up'."
    )
    permission = ToolPermission.READ
    parameters = {
        "type": "object",
        "properties": {"server": {"type": "string", "description": "Server name, or empty for all."}},
        "required": [],
    }

    def __init__(self, siris: "SirisAI") -> None:
        self._siris = siris

    def run(self, server: str = "", **kwargs: Any) -> Any:
        hosts = _targets(self._siris, server or "all")
        out = []
        for host in hosts:
            try:
                out.append(watch.permissions(self._siris, host))
            except SshError as exc:
                out.append({"server": host.name, "error": str(exc)})
        return out


class SshRebootNeededTool(Tool):
    """6.38: reboot the servers that need it, one at a time."""

    name = "ssh_reboot_needed"
    description = (
        "Reboot the servers that need a reboot after updates (or the ones named), one at a time: each has to be back "
        "before the next goes down, SirisAI's own server last, and it stops if one doesn't come back. Now, or at a set "
        "time ('reboot whatever needs it at 3am'). Always asks first. Use for 'reboot the servers that need it'."
    )
    permission = ToolPermission.CONFIRM_REQUIRED
    parameters = {
        "type": "object",
        "properties": {
            "servers": {"type": "string", "description": "Optional: these servers instead of the ones that need it."},
            "at": {"type": "string", "description": "Optional: when, e.g. '03:00' or 'sunday 2am' (local)."},
        },
        "required": [],
    }

    def __init__(self, siris: "SirisAI") -> None:
        self._siris = siris

    def run(self, servers: str = "", at: str = "", **kwargs: Any) -> Any:
        from app.core import clock
        from app.servers import power, watch

        if self._siris.tools.get("ssh_power") is None or not getattr(self._siris.tools.get("ssh_power"), "_allowed", False):
            raise PermissionError("Rebooting servers is turned off (SIRISAI_SSH_ALLOW_POWER / SIRISAI_SSH_ALLOW_CHANGES).")
        names = [h.name for h in _targets(self._siris, servers)] if servers.strip() else watch.needs_reboot(self._siris)
        if not names:
            return {"servers": [], "summary": "No server needs a reboot right now."}
        if self._siris.state.get(power.ROLLING_KEY):
            raise ValueError("A one-at-a-time reboot is already under way.")
        if at:
            when = power.parse_when(self._siris, at)
            power.schedule(self._siris, "rolling", names, when)
            local = when.astimezone(clock.get_zone(self._siris.settings.timezone))
            return {"servers": names, "at": when.isoformat(),
                    "summary": f"At {local.strftime('%a %-d %b %H:%M')} I'll reboot {', '.join(names)}, one at a time."}
        order = power.start_rolling(self._siris, names)["queue"]
        power.tick(self._siris)
        return {"servers": order, "summary": f"Rebooting {', '.join(order)}, one at a time; I'll say when they're all back."}


class SshPowerScheduledTool(Tool):
    """6.38: what reboots are coming up."""

    name = "ssh_power_scheduled"
    description = "The reboots and shutdowns set for later, and any one-at-a-time reboot under way. Cancel one with ssh_power action cancel."
    permission = ToolPermission.READ
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, siris: "SirisAI") -> None:
        self._siris = siris

    def run(self, **kwargs: Any) -> Any:
        from app.servers import power

        return {"scheduled": power.scheduled(self._siris), "rolling": self._siris.state.get(power.ROLLING_KEY),
                "following_up": power.pending(self._siris)}


class ServerTimelineTool(Tool):
    """6.38: what happened to a server."""

    name = "server_timeline"
    description = (
        "What happened to a server lately, newest first: reboots (and whether it came back), updates installed, failed "
        "services, full disks, not answering, disk trouble and its cron jobs' runs. For 'what happened to the Pi this week', "
        "'when did the NAS last reboot'. days defaults to 7."
    )
    permission = ToolPermission.READ
    parameters = {
        "type": "object",
        "properties": {"server": {"type": "string"}, "days": {"type": "integer", "description": "How far back (1-30)."}},
        "required": ["server"],
    }

    def __init__(self, siris: "SirisAI") -> None:
        self._siris = siris

    def run(self, server: str = "", days: int = 7, **kwargs: Any) -> Any:
        from app.servers import timeline

        name = server.strip()
        if self._siris.ssh_client is not None:
            try:
                name = self._siris.ssh_client.find(server).name
            except ValueError:
                pass
        days = max(1, min(int(days or 7), 30))
        return {"server": name, "events": timeline.events(self._siris, name, days), "text": timeline.text(self._siris, name, days)}


class DiskForecastTool(Tool):
    """6.58: when each disk will be full at the rate it's filling."""

    name = "disk_forecast"
    description = ("When each server's disks will be full at the rate they're filling, from a daily record of their size. "
                   "For 'when will the NAS fill up', 'how fast is the media disk growing'.")
    permission = ToolPermission.READ
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, siris: "SirisAI") -> None:
        self._siris = siris

    def run(self, **kwargs: Any) -> Any:
        from app.servers import forecast

        return {"disks": forecast.forecasts(self._siris), "text": forecast.text(self._siris)}


class NetworkMapTool(Tool):
    """6.58: what runs where."""

    name = "network_map"
    description = ("What runs where on the home network: each server, the containers on it, and which services (Home Assistant, "
                   "Plex, SirisOS, Ollama...) it hosts. what is given, instead says what depends on that server, container or "
                   "service: 'what breaks if the NAS goes down'.")
    permission = ToolPermission.READ
    parameters = {"type": "object", "properties": {"what": {"type": "string", "description": "A server, container or service name"}}, "required": []}

    def __init__(self, siris: "SirisAI") -> None:
        self._siris = siris

    def run(self, what: str = "", **kwargs: Any) -> Any:
        from app.homelab import network_map

        if what.strip():
            return network_map.depends_on(self._siris, what)
        return {**network_map.build(self._siris), "text": network_map.text(self._siris)}
