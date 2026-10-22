"""SSH tools (app/ssh/client.py): reach the servers on the home network.

- ssh_list_servers (READ): what's configured.
- ssh_server_health (READ): a fixed health snapshot -- load, memory, disks,
  failed services -- with plain-English warnings.
- ssh_run (CONFIRM_REQUIRED): run a command. Look-only commands (uptime,
  df, systemctl status, journalctl...) skip the question; anything else is
  shown to the user, host and command, for Allow/Cancel first. With
  SIRISAI_SSH_ALLOW_CHANGES=false only look-only commands run at all.

Server output is external text: the injection guard (app/core/taint.py)
treats it as untrusted, so a log line can't instruct SirisAI to act.
"""

from typing import Any

from app.plugins.base import Tool, ToolPermission
from app.ssh.client import HEALTH_COMMAND, SshClient, SshError, is_read_only, parse_health


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
