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

import logging
import re
from collections.abc import Callable
from typing import Any

from app.plugins.base import Tool, ToolPermission
from app.ssh.client import (
    BOOT_ID_COMMAND,
    HEALTH_COMMAND,
    NEEDS_SUDO_EXIT,
    SshClient,
    SshError,
    is_read_only,
    parse_health,
    power_command,
    power_started,
)

logger = logging.getLogger(__name__)
_BOOT_ID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


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
        "comma-separated list, or 'all'. at sets a time ('reboot the NAS at 3am Sunday'); SirisAI does it then and checks it came back."
    )
    permission = ToolPermission.CONFIRM_REQUIRED
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["reboot", "shutdown", "cancel"]},
            "servers": {"type": "string", "description": "A server name, several separated by commas, or 'all'."},
            "delay_minutes": {"type": "integer", "description": "Wait this many minutes first (0-240). Defaults to now."},
            "at": {"type": "string", "description": "Or at a set time instead: '03:00', 'sunday 3am', 'tomorrow 02:30' or an ISO time (local)."},
        },
        "required": ["action", "servers"],
    }

    def __init__(self, client: SshClient, self_server: str = "", allowed: bool = True,
                 on_started: Callable[..., None] | None = None, siris: Any = None) -> None:
        self._siris = siris
        self._client = client
        self._self = self_server.strip().lower()
        self._allowed = allowed
        self._on_started = on_started

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
        at = kwargs.get("at")
        if at and action != "cancel":
            return self._schedule(action, servers, str(at))
        results = []
        for host in self._targets(servers):
            command = power_command(host, action, delay)
            boot_id = self._boot_id(host.name) if action != "cancel" and not delay else None
            try:
                out = self._client.run(host.name, command, timeout=20)
            except SshError as exc:
                if action != "cancel" and not delay and boot_id and power_started(str(exc)):
                    # It went down under us: that's the reboot starting.
                    results.append({"server": host.name, "status": "started", "self": host.name == self._self})
                    self._started(host.name, action, boot_id)
                else:
                    results.append({"server": host.name, "status": "failed", "detail": str(exc)})
                continue
            lines = out["stdout"].strip().splitlines()
            if lines and _BOOT_ID_RE.fullmatch(lines[0].strip()):
                boot_id = lines.pop(0).strip()
            if out["exit_code"] == NEEDS_SUDO_EXIT:
                results.append({"server": host.name, "status": "failed", "detail": f"{host.user} needs passwordless sudo for this (see deploy/README.md: SSH to your other servers)"})
            elif out["exit_code"] != 0:
                results.append({"server": host.name, "status": "failed", "detail": (out["stderr"] or out["stdout"]).strip()[-300:] or f"exit {out['exit_code']}"})
            else:
                status = lines[-1].strip() if lines else "started"
                results.append({"server": host.name, "status": status, "self": host.name == self._self})
                if action != "cancel":
                    self._started(host.name, action, boot_id, delay)
        done = [r["server"] for r in results if r["status"] != "failed"]
        summary = {
            "reboot": f"Rebooting {', '.join(done)}" + (f" in {delay} minutes" if delay else ""),
            "shutdown": f"Shutting down {', '.join(done)}" + (f" in {delay} minutes" if delay else ""),
            "cancel": f"Cancelled the pending shutdown on {', '.join(done)}",
        }[action] if done else "Nothing was done."
        if done and action != "cancel" and self._on_started:
            summary += ". I'll tell you when " + ("they're" if len(done) > 1 else "it's") + (" back." if action == "reboot" else " off.")
        if action == "cancel" and self._siris is not None:
            from app.servers import power

            dropped = power.cancel_scheduled(self._siris, [h.name for h in self._targets(servers)])
            if dropped:
                names = sorted({s for d in dropped for s in d["servers"]})
                summary = (summary if done else "") + ("; " if done else "") + f"Cancelled the scheduled reboot of {', '.join(names)}"
        failed = [r for r in results if r["status"] == "failed"]
        if failed:
            summary += (". " if done else " ") + "; ".join(f"{r['server']}: {r['detail']}" for r in failed)
        return {"action": action, "delay_minutes": delay, "results": results, "summary": summary}

    def _schedule(self, action: str, servers: str, at: str) -> dict[str, Any]:
        from app.core import clock
        from app.servers import power

        if self._siris is None:
            raise ValueError("A set time isn't available here; use delay_minutes.")
        names = [h.name for h in self._targets(servers)]
        when = power.parse_when(self._siris, at)
        entry = power.schedule(self._siris, action, names, when)
        local = when.astimezone(clock.get_zone(self._siris.settings.timezone))
        verb = "Rebooting" if action == "reboot" else "Shutting down"
        return {"action": action, "scheduled": entry, "results": [],
                "summary": f"{verb} {', '.join(names)} at {local.strftime('%a %-d %b %H:%M')}. I'll check "
                           + ("they come back." if len(names) > 1 else "it comes back.") if action == "reboot" else
                           f"{verb} {', '.join(names)} at {local.strftime('%a %-d %b %H:%M')}."}

    def _boot_id(self, server: str) -> str | None:
        try:
            out = self._client.run(server, BOOT_ID_COMMAND, timeout=10)
        except SshError:
            return None
        text = out["stdout"].strip()
        return text if _BOOT_ID_RE.fullmatch(text) else None

    def _started(self, server: str, action: str, boot_id: str | None, delay: int = 0) -> None:
        if self._on_started is None:
            return
        try:
            self._on_started(server=server, action=action, boot_id=boot_id, delay_minutes=delay)
        except Exception:  # noqa: BLE001 - following it up is extra; the reboot itself went
            logger.exception("Couldn't keep track of the %s on %s", action, server)
