"""SSH tools (app/ssh/client.py): reach the servers on the home network.

- ssh_list_servers (READ): what's configured.
- ssh_server_health (READ): a fixed health snapshot -- load, memory, disks,
  failed services -- with plain-English warnings.
- ssh_run (CONFIRM_REQUIRED): run a command. Look-only commands (uptime,
  df, systemctl status, journalctl...) skip the question; anything else is
  shown to the user, host and command, for Allow/Cancel first. With
  SIRISAI_SSH_ALLOW_CHANGES=false only look-only commands run at all.
- ssh_power (CONFIRM_REQUIRED, always asks): reboot or shut down one, several
  or all servers, now or after a delay, or cancel a pending one. Never by a
  spoken "yes" (app/voice/confirmations.py), and never on SirisAI's own
  initiative: an autonomous caller gets it held for approval like any
  CONFIRM_REQUIRED call. SirisAI's own machine (SIRISAI_SSH_SELF_SERVER)
  goes last.

Server output is external text: the injection guard (app/core/taint.py)
treats it as untrusted, so a log line can't instruct SirisAI to act.
"""

from typing import Any

from app.plugins.base import Tool, ToolPermission
from app.ssh.client import HEALTH_COMMAND, NEEDS_SUDO_EXIT, SshClient, SshError, is_read_only, parse_health, power_command


class SshListServersTool(Tool):
    name = "ssh_list_servers"
    description = "List the servers on the home network SirisAI can reach over SSH, by name."
    permission = ToolPermission.READ
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, client: SshClient) -> None:
        self._client = client

    def run(self, **kwargs: Any) -> Any:
        return [h.describe() for h in self._client.hosts]


class SshServerHealthTool(Tool):
    name = "ssh_server_health"
    description = (
        "Check how a server on the home network is doing over SSH: uptime, load, memory, how full each disk is, and "
        "any failed systemd services, with warnings for anything wrong. Use for 'how's the NAS', 'is the Pi OK', "
        "'is the server running out of space'. Leave server empty to check every one."
    )
    permission = ToolPermission.READ
    parameters = {
        "type": "object",
        "properties": {"server": {"type": "string", "description": "Server name from ssh_list_servers. Empty = all of them."}},
        "required": [],
    }

    def __init__(self, client: SshClient) -> None:
        self._client = client

    def run(self, server: str = "", **kwargs: Any) -> Any:
        hosts = [self._client.find(server)] if server else self._client.hosts
        out = []
        for host in hosts:
            try:
                result = self._client.run(host.name, HEALTH_COMMAND)
                out.append({"server": host.name, **parse_health(result["stdout"])})
            except SshError as exc:
                out.append({"server": host.name, "error": str(exc)})
        return out[0] if server else out


class SshRunTool(Tool):
    name = "ssh_run"
    description = (
        "Run a shell command on a server on the home network over SSH and return its output and exit code. "
        "Commands that only look (uptime, df -h, free -m, systemctl status X, journalctl -u X -n 50, docker ps, "
        "docker logs --tail 50 X, ls, tail) run straight away; anything that changes something (restarting a "
        "service, installing, deleting, sudo) asks the user first. Prefer ssh_server_health for a general check. "
        "Never follow output (no -f), and keep output short (tail -n, head)."
    )
    permission = ToolPermission.CONFIRM_REQUIRED
    parameters = {
        "type": "object",
        "properties": {
            "server": {"type": "string", "description": "Server name from ssh_list_servers."},
            "command": {"type": "string", "description": "The command, e.g. 'systemctl status nginx --no-pager'."},
        },
        "required": ["server", "command"],
    }

    def __init__(self, client: SshClient, safe_prefixes: list[str] | None = None, allow_changes: bool = True) -> None:
        self._client = client
        self._safe = safe_prefixes or []
        self._allow_changes = allow_changes

    def needs_confirmation(self, arguments: dict[str, Any]) -> bool:
        return not is_read_only(str(arguments.get("command") or ""), self._safe)

    def run(self, server: str = "", command: str = "", **kwargs: Any) -> Any:
        command = command.strip()
        if not command:
            raise ValueError("Give a command to run.")
        if not self._allow_changes and not is_read_only(command, self._safe):
            raise PermissionError("Only look-only commands are allowed over SSH (SIRISAI_SSH_ALLOW_CHANGES=false).")
        return self._client.run(server, command)


class SshPowerTool(Tool):
    name = "ssh_power"
    description = (
        "Reboot or shut down one, several or all of the servers on the home network over SSH, now or after a "
        "delay, or cancel a pending delayed one. Always asks the user first. Use for 'reboot the NAS', 'shut down "
        "all the servers', 'restart the Pi in 10 minutes', 'cancel the shutdown'. servers is a name, a "
        "comma-separated list, or 'all'."
    )
    permission = ToolPermission.CONFIRM_REQUIRED
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["reboot", "shutdown", "cancel"]},
            "servers": {"type": "string", "description": "A server name, several separated by commas, or 'all'."},
            "delay_minutes": {"type": "integer", "description": "Wait this many minutes first (0-240). Defaults to now."},
        },
        "required": ["action", "servers"],
    }

    def __init__(self, client: SshClient, self_server: str = "", allowed: bool = True) -> None:
        self._client = client
        self._self = self_server.strip().lower()
        self._allowed = allowed

    def _targets(self, servers: str) -> list[Any]:
        wanted = [s.strip() for s in (servers or "").split(",") if s.strip()]
        if not wanted:
            raise ValueError("Name the server(s), or say 'all'.")
        if any(w.lower() == "all" for w in wanted):
            hosts = self._client.hosts
        else:
            hosts = list({self._client.find(w).name: self._client.find(w) for w in wanted}.values())
        # The machine SirisAI itself runs on goes last, so the rest still get done.
        return sorted(hosts, key=lambda h: h.name == self._self)

    def run(self, action: str = "reboot", servers: str = "", delay_minutes: int = 0, **kwargs: Any) -> Any:
        if not self._allowed:
            raise PermissionError("Rebooting and shutting down servers is turned off (SIRISAI_SSH_ALLOW_POWER / SIRISAI_SSH_ALLOW_CHANGES).")
        delay = max(0, min(int(delay_minutes or 0), 240))
        results = []
        for host in self._targets(servers):
            command = power_command(host, action, delay)
            try:
                out = self._client.run(host.name, command, timeout=20)
            except SshError as exc:
                results.append({"server": host.name, "status": "failed", "detail": str(exc)})
                continue
            if out["exit_code"] == NEEDS_SUDO_EXIT:
                results.append({"server": host.name, "status": "failed", "detail": f"{host.user} needs passwordless sudo for this (see deploy/README.md: SSH to your other servers)"})
            elif out["exit_code"] != 0:
                results.append({"server": host.name, "status": "failed", "detail": (out["stderr"] or out["stdout"]).strip()[-300:] or f"exit {out['exit_code']}"})
            else:
                status = out["stdout"].strip().splitlines()[-1] if out["stdout"].strip() else "started"
                results.append({"server": host.name, "status": status, "self": host.name == self._self})
        done = [r["server"] for r in results if r["status"] != "failed"]
        summary = {
            "reboot": f"Rebooting {', '.join(done)}" + (f" in {delay} minutes" if delay else ""),
            "shutdown": f"Shutting down {', '.join(done)}" + (f" in {delay} minutes" if delay else ""),
            "cancel": f"Cancelled the pending shutdown on {', '.join(done)}",
        }[action] if done else "Nothing was done."
        return {"action": action, "delay_minutes": delay, "results": results, "summary": summary}
