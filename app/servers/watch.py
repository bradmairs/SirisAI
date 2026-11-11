"""Watching the servers on the home network, so problems show up in the
attention feed before you notice them.

- **Health** (every SIRISAI_SERVER_WATCH_MINUTES, 15): load, memory, disks
  and failed systemd services on each server (ssh_server_health's snapshot).
  A disk past SIRISAI_SERVER_DISK_WARN_PERCENT (90), memory at 95%+, or a
  failed service each raise an item; a failed service has **Restart** (one
  tap, then `systemctl restart`). A server that misses two checks in a row
  raises "nas isn't answering", with **Wake** when its MAC is known. Items
  close themselves when the problem goes away. Each check also learns the
  server's MAC address for Wake-on-LAN (app/servers/wake.py).
- **OS updates** (daily at SIRISAI_SERVER_UPDATES_HOUR_LOCAL, 7am): "12
  updates waiting on nas (2 security)" with **Install updates**, and "nas
  needs a reboot" (after a kernel update, say) with **Reboot**. apt and dnf.
- **Disk health** (daily at SIRISAI_SERVER_DISKS_HOUR_LOCAL, 6am): SMART
  health and the warning counters (reallocated or pending sectors, NVMe wear
  and media errors), degraded ZFS pools and software RAID arrays.

Restart, Install updates and Reboot run with the user's tap as the
confirmation. A non-root account needs passwordless sudo for each
(`server_permissions` says what's missing and the sudoers line to add).
"""

import logging
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from app.hub.attention import action, register_action
from app.servers import checks, wake
from app.ssh.client import HEALTH_COMMAND, SshError, SshHost, parse_health, passwordless, sudo_prefix

if TYPE_CHECKING:
    from app.core.siris import SirisAI

logger = logging.getLogger(__name__)

STATE_KEY = "server_watch"
DOWN_AFTER = 2


def _hosts(siris: "SirisAI") -> list[SshHost]:
    return siris.ssh_client.hosts if siris.ssh_client is not None else []


def check_health(siris: "SirisAI", now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    state = dict(siris.state.get(STATE_KEY) or {})
    warn = siris.settings.server_disk_warn_percent
    wanted: list[dict[str, Any]] = []
    report: dict[str, Any] = {}
    for host in _hosts(siris):
        entry = dict(state.get(host.name) or {})
        try:
            result = siris.ssh_client.run(host.name, HEALTH_COMMAND, timeout=20)  # type: ignore[union-attr]
        except SshError as exc:
            entry["failures"] = int(entry.get("failures", 0)) + 1
            entry["last_error"] = str(exc)[:300]
            state[host.name] = entry
            report[host.name] = {"reachable": False, "error": entry["last_error"]}
            if entry["failures"] >= DOWN_AFTER:
                mac = wake.mac_for(siris, host)
                last_ok = entry.get("last_ok")
                wanted.append(
                    {
                        "key": f"servers:down:{host.name}",
                        "kind": "alert",
                        "title": f"{host.name} isn't answering",
                        "body": f"{entry['last_error']}" + (f" Last seen {last_ok[:16].replace('T', ' ')} UTC." if last_ok else ""),
                        "severity": "attention",
                        "actions": ([action("wake", "Wake", "primary")] if mac else []) + [action("acknowledge", "Got it")],
                        "payload": {"server": host.name},
                    }
                )
            continue
        health = parse_health(result["stdout"])
        entry.update(failures=0, last_ok=now.isoformat(), last_error=None)
        state[host.name] = entry
        if health.get("mac"):
            wake.remember_mac(siris, host, health["mac"])
        report[host.name] = {"reachable": True, **health}
        for disk in health.get("disks", []):
            if disk["used_percent"] >= warn:
                wanted.append(
                    {
                        "key": f"servers:disk:{host.name}:{disk['mount']}",
                        "kind": "alert",
                        "title": f"{disk['mount']} on {host.name} is {disk['used_percent']}% full",
                        "body": f"{disk['free']} free of {disk['size']}.",
                        "severity": "urgent" if disk["used_percent"] >= 97 else "attention",
                        "actions": [action("acknowledge", "Got it")],
                    }
                )
        memory = (health.get("memory") or {}).get("used_percent")
        if memory is not None and memory >= 95:
            wanted.append(
                {
                    "key": f"servers:memory:{host.name}",
                    "kind": "alert",
                    "title": f"{host.name} is out of memory ({memory}% used)",
                    "body": "Something may be leaking or swapping hard.",
                    "severity": "attention",
                    "actions": [action("acknowledge", "Got it")],
                }
            )
        for unit in health.get("failed_services", []):
            wanted.append(
                {
                    "key": f"servers:failed:{host.name}:{unit}",
                    "kind": "alert",
                    "title": f"{unit} has failed on {host.name}",
                    "body": f"systemd reports {unit} as failed. Restart it, or look at `journalctl -u {unit}` first.",
                    "severity": "attention",
                    "actions": [action("restart", "Restart", "primary", confirm=f"Restart {unit} on {host.name}?"), action("dismiss", "Leave it")],
                    "payload": {"server": host.name, "unit": unit},
                }
            )
    siris.state.set(STATE_KEY, state)
    siris.attention.sync("servers", wanted, outcome="Cleared up")
    return report


def check_updates(siris: "SirisAI", now: datetime | None = None) -> dict[str, Any]:
    wanted: list[dict[str, Any]] = []
    report: dict[str, Any] = {}
    for host in _hosts(siris):
        try:
            result = siris.ssh_client.run(host.name, checks.updates_command(), timeout=60)  # type: ignore[union-attr]
        except SshError as exc:
            report[host.name] = {"error": str(exc)[:300]}
            continue
        found = checks.parse_updates(result["stdout"])
        report[host.name] = found
        if found["updates"]:
            security = f" ({found['security']} security)" if found["security"] else ""
            wanted.append(
                {
                    "key": f"server-updates:{host.name}",
                    "kind": "suggestion",
                    "title": f"{found['updates']} update{'s' if found['updates'] != 1 else ''} waiting on {host.name}{security}",
                    "body": ", ".join(found["packages"][:12]) + ("..." if found["updates"] > 12 else ""),
                    "severity": "attention" if found["security"] else "info",
                    "actions": [action("install", "Install updates", "primary", confirm=f"Install {found['updates']} updates on {host.name}?"), action("dismiss", "Not now")],
                    "payload": {"server": host.name, "package_manager": found["package_manager"]},
                }
            )
        if found["reboot_required"]:
            because = f" for {', '.join(found['reboot_because'][:4])}" if found["reboot_because"] else ""
            wanted.append(
                {
                    "key": f"server-updates:reboot:{host.name}",
                    "kind": "suggestion",
                    "title": f"{host.name} needs a reboot",
                    "body": f"Updates installed on {host.name} only take effect after a reboot{because}.",
                    "severity": "info",
                    "actions": [action("reboot", "Reboot", "primary", confirm=f"Reboot {host.name} now?"), action("dismiss", "Later")],
                    "payload": {"server": host.name},
                }
            )
        last = found.get("last_install")
        if last and last.get("exit_code") not in (0, None):
            wanted.append(
                {
                    "key": f"server-updates:failed:{host.name}",
                    "kind": "alert",
                    "title": f"Installing updates on {host.name} failed",
                    "body": install_failure(host, last),
                    "severity": "attention",
                    "actions": [action("acknowledge", "Got it")],
                }
            )
    siris.attention.sync("server-updates", wanted, outcome="Up to date")
    return report


def check_disks(siris: "SirisAI", now: datetime | None = None) -> dict[str, Any]:
    wanted: list[dict[str, Any]] = []
    report: dict[str, Any] = {}
    for host in _hosts(siris):
        try:
            result = siris.ssh_client.run(host.name, checks.disks_command(host), timeout=60)  # type: ignore[union-attr]
        except SshError as exc:
            report[host.name] = {"error": str(exc)[:300]}
            continue
        found = checks.parse_disks(result["stdout"])
        report[host.name] = found
        for problem in found["problems"]:
            wanted.append(
                {
                    "key": f"server-disks:{host.name}:{problem.split(':')[0]}",
                    "kind": "alert",
                    "title": f"Disk trouble on {host.name}: {problem}",
                    "body": "A drive reporting errors can fail soon. Check your backups of anything on it, and plan to replace it.",
                    "severity": "urgent" if "FAIL" in problem.upper() or "DEGRADED" in problem.upper() else "attention",
                    "actions": [action("acknowledge", "Got it")],
                }
            )
    siris.attention.sync("server-disks", wanted, outcome="Disks healthy again")
    return report


# Each feature's sudo probe (exactly the command it runs) and the sudoers entry
# that allows it. Exact commands, no wildcards: sudo-rs (Ubuntu 25.10's sudo)
# doesn't accept wildcards in arguments, and exact rules work on both.
# Restarting a service needs one rule per service, so it isn't listed here;
# a failed restart says the exact line for that service.
CAPABILITIES: dict[str, tuple[str, str]] = {
    "reboot": ("systemctl reboot", "/usr/bin/systemctl reboot"),
    "shut down": ("systemctl poweroff", "/usr/bin/systemctl poweroff"),
    "delayed power / cancel": ("shutdown", "/usr/sbin/shutdown"),
    "install updates (apt)": (
        checks.APT_UPGRADE,
        "/usr/bin/apt-get -y -q update, /usr/bin/apt-get " + checks.APT_UPGRADE.removeprefix("apt-get ").replace(":", "\\:").replace("=", "\\="),
    ),
    "read SMART": ("smartctl", "/usr/sbin/smartctl"),
}


def update_rule(host: SshHost) -> str:
    return f"{host.user} ALL=(root) NOPASSWD: {CAPABILITIES['install updates (apt)'][1]}"


def install_failure(host: SshHost, last: dict[str, Any]) -> str:
    """Why the last install failed, in words: sudo wanting a password says which rule to add."""
    tail = (last.get("log_tail") or "").strip()
    if re.search(r"password is required|authentication is required|a terminal is required", tail, re.I):
        return (f"{host.user} needs passwordless sudo for apt-get on {host.name}. On {host.name}, run "
                f"`sudo visudo -f /etc/sudoers.d/sirisai` and add: {update_rule(host)}")
    return tail or f"It stopped with exit code {last.get('exit_code')}, and its log ({checks.UPDATE_LOG}) is gone from {host.name}."


def restart_rule(host: SshHost, unit: str) -> str:
    return f"{host.user} ALL=(root) NOPASSWD: /usr/bin/systemctl restart {unit}"


def permissions(siris: "SirisAI", host: SshHost) -> dict[str, Any]:
    """What this account can do through sudo without a password, and the
    sudoers line that would allow the rest. Read-only."""
    names = list(CAPABILITIES)
    if not sudo_prefix(host):
        return {"server": host.name, "user": host.user, "root": True, "can": names, "cannot": [], "sudoers": None}
    probe = "; ".join(f"{passwordless(cmd)} && echo 'ok {i}' || echo 'no {i}'" for i, (cmd, _) in enumerate(CAPABILITIES.values()))
    result = siris.ssh_client.run(host.name, probe, timeout=20)  # type: ignore[union-attr]
    allowed = {int(line.split()[1]) for line in result["stdout"].splitlines() if line.startswith("ok ")}
    cannot = [names[i] for i in range(len(names)) if i not in allowed]
    line = f"{host.user} ALL=(root) NOPASSWD: " + ", ".join(CAPABILITIES[n][1] for n in cannot) if cannot else None
    return {
        "server": host.name,
        "user": host.user,
        "root": False,
        "can": [n for n in names if n not in cannot],
        "cannot": cannot,
        "sudoers": line,
        "restart_services": f"Restarting a service needs a rule per service, e.g. {restart_rule(host, 'nginx.service')}",
        "check_paths": "Check the paths with `which systemctl shutdown apt-get smartctl` on the server.",
    }


# -- feed actions --------------------------------------------------------------


def _restart(siris: "SirisAI", item: dict[str, Any], payload: dict[str, Any], trigger: str) -> tuple[Any, str]:
    host = siris.ssh_client.find(payload["server"])  # type: ignore[union-attr]
    result = siris.run_tool("ssh_run", {"server": host.name, "command": checks.restart_unit_command(host, payload["unit"])}, confirmed=True, trigger=trigger)
    if checks.needs_sudo(result):
        raise PermissionError(f"{host.user} on {host.name} needs passwordless sudo to restart {payload['unit']}. Add this to /etc/sudoers.d/sirisai there: {restart_rule(host, payload['unit'])}")
    if result["exit_code"] != 0:
        raise RuntimeError((result["stderr"] or result["stdout"]).strip()[-300:] or f"exit {result['exit_code']}")
    return result, f"Restarted {payload['unit']}"


def _wake(siris: "SirisAI", item: dict[str, Any], payload: dict[str, Any], trigger: str) -> tuple[Any, str]:
    result = siris.run_tool("ssh_wake", {"servers": payload["server"]}, confirmed=True, trigger=trigger)
    return result, "Wake-up sent"


def _install(siris: "SirisAI", item: dict[str, Any], payload: dict[str, Any], trigger: str) -> tuple[Any, str]:
    result = siris.run_tool("ssh_install_updates", {"server": payload["server"]}, confirmed=True, trigger=trigger)
    return result, "Installing updates"


def _reboot(siris: "SirisAI", item: dict[str, Any], payload: dict[str, Any], trigger: str) -> tuple[Any, str]:
    result = siris.run_tool("ssh_power", {"action": "reboot", "servers": payload["server"]}, confirmed=True, trigger=trigger)
    if not any(r.get("status") != "failed" for r in result.get("results", [])):
        # Fixed in 5.73: this used to close the item as done with "Nothing was done."
        raise RuntimeError(result.get("summary") or "The reboot didn't start.")
    return result, result.get("summary", "Rebooting")


register_action("servers", "restart", _restart)
register_action("servers", "wake", _wake)
register_action("servers-power", "wake", _wake)
register_action("server-updates", "install", _install)
register_action("server-updates", "reboot", _reboot)
