"""SSH to the servers on the home network (SIRISAI_SSH_HOSTS): run a command
on one, or take a quick health snapshot of it.

Same approach as the cron overview (app/cron/client.py): it shells out to
the system `ssh` binary, so it uses whatever identity file, agent and
known_hosts SirisAI's own user already has (in deploy/, data/sirisai/ssh
mounted at /root/.ssh), rather than managing keys itself. BatchMode means a
host it has no key for fails fast instead of waiting for a password.

Only configured hosts can be reached; a name the model makes up is refused.
Whether a command needs the user's OK is decided here (`is_read_only`), not
by the model: a short list of look-but-don't-touch commands (uptime, df,
free, systemctl status, journalctl, docker ps...) with no shell operators
runs straight away; anything else is asked about first, showing the exact
host and command. Reading files that look like secrets (keys, .env,
shadow...) is never treated as read-only.
"""

import re
import shlex
import subprocess
import time
from dataclasses import dataclass
from typing import Any

MAX_OUTPUT_CHARS = 6000


@dataclass(frozen=True)
class SshHost:
    name: str
    user: str
    host: str
    port: int = 22

    @property
    def target(self) -> str:
        return f"{self.user}@{self.host}" if self.user else self.host

    def describe(self) -> dict[str, Any]:
        return {"name": self.name, "target": self.target, "port": self.port}


def parse_ssh_hosts(spec: str) -> list[SshHost]:
    """SIRISAI_SSH_HOSTS: comma-separated "name=user@host[:port]" entries,
    e.g. "nas=brad@192.168.0.127,pi=root@192.168.0.83:2222". The name is
    optional (it defaults to the host)."""
    hosts = []
    for entry in spec.split(","):
        entry = entry.strip()
        if not entry:
            continue
        name, sep, target = entry.partition("=")
        if not sep:
            name, target = "", entry
        user, at, host_port = target.strip().rpartition("@")
        if not at:
            user, host_port = "", target.strip()
        host, _, port = host_port.partition(":")
        if not host:
            raise ValueError(f"Bad SIRISAI_SSH_HOSTS entry: {entry!r}")
        hosts.append(SshHost(name=(name.strip() or host).lower(), user=user, host=host, port=int(port) if port else 22))
    return hosts


# Commands that only look. A command counts as read-only when its program is
# here (and, where listed, its first argument too) and it has no shell
# operators, so nothing can be chained, redirected or substituted onto it.
_READ_ONLY: dict[str, set[str] | None] = {
    "uptime": None, "uname": None, "hostname": None, "whoami": None, "id": None, "date": None,
    "df": None, "du": None, "free": None, "lsblk": None, "findmnt": None, "mount": None,
    "ls": None, "stat": None, "cat": None, "head": None, "tail": None, "wc": None, "grep": None,
    "ps": None, "pgrep": None, "vmstat": None, "iostat": None, "mpstat": None, "sensors": None,
    "nvidia-smi": None, "who": None, "w": None, "last": None, "dmesg": None, "lscpu": None,
    "ip": {"a", "addr", "address", "r", "route", "link", "neigh", "-br", "-4", "-6"},
    "ss": None, "ping": None, "dig": None, "nslookup": None, "host": None,
    "journalctl": None,
    "systemctl": {"status", "is-active", "is-enabled", "is-failed", "list-units", "list-timers", "list-unit-files", "--failed"},
    # Not `docker inspect` or `systemctl show`: both print environment variables, often API keys.
    "docker": {"ps", "logs", "stats", "images", "version", "info", "top"},
    "zpool": {"status", "list", "iostat"}, "zfs": {"list", "get"},
    "apt": {"list"}, "dpkg": {"-l", "--list"}, "uptimed": None, "top": {"-b", "-bn1", "-bn", "-n"},
}
_SHELL_OPERATORS = re.compile(r"[;&|<>`$\n\\]|\(|\)")
_SECRETISH = re.compile(r"(environ|_history|\.ssh|id_rsa|id_ed25519|id_ecdsa|shadow|gshadow|\.env\b|secret|token|passw|\.pem\b|\.key\b|credential|\.netrc|\.pgpass|htpasswd)", re.IGNORECASE)
_FOLLOW_FLAGS = {"-f", "--follow", "-F"}
_ONLY_BARE = {"hostname", "date", "mount"}


def is_read_only(command: str, extra_prefixes: list[str] | None = None) -> bool:
    command = command.strip()
    if not command or _SHELL_OPERATORS.search(command) or _SECRETISH.search(command):
        return False
    try:
        words = shlex.split(command)
    except ValueError:
        return False
    if not words or words[0] in ("sudo", "su", "doas") or _FOLLOW_FLAGS & set(words):
        return False
    for prefix in extra_prefixes or []:
        wanted = shlex.split(prefix)
        if wanted and words[: len(wanted)] == wanted:
            return True
    if words[0] not in _READ_ONLY:
        return False
    allowed = _READ_ONLY[words[0]]
    if words[0] == "ping" and not ({"-c"} & set(words)):
        return False  # without a count it never stops
    if words[0] == "docker" and words[1:2] == ["stats"] and "--no-stream" not in words:
        return False
    if words[0] in _ONLY_BARE and len(words) > 1:
        return False  # `hostname NAME`, `date -s ...` and `mount DEV DIR` change things
    if words[0] == "ps" and any("e" in w for w in words[1:] if not w.startswith("-")):
        return False  # BSD-style `ps e` prints other processes' environments
    if words[0] == "ip" and {"set", "add", "del", "delete", "flush", "change", "replace", "append"} & set(words):
        return False
    return allowed is None or (len(words) > 1 and words[1] in allowed)


class SshError(RuntimeError):
    pass


class SshClient:
    def __init__(self, hosts: list[SshHost], timeout: float = 30.0, connect_timeout: float = 8.0, ssh_binary: str = "ssh") -> None:
        self._hosts = {h.name: h for h in hosts}
        self._timeout = timeout
        self._connect_timeout = connect_timeout
        self._ssh_binary = ssh_binary

    @property
    def hosts(self) -> list[SshHost]:
        return list(self._hosts.values())

    def find(self, name: str) -> SshHost:
        key = (name or "").strip().lower()
        if key in self._hosts:
            return self._hosts[key]
        for host in self._hosts.values():
            if key in (host.host.lower(), host.target.lower()):
                return host
        known = ", ".join(self._hosts) or "none"
        raise ValueError(f"Unknown server '{name}'. Configured: {known}.")

    def _args(self, host: SshHost, command: str) -> list[str]:
        return [
            self._ssh_binary,
            "-p", str(host.port),
            "-o", "BatchMode=yes",
            "-o", f"ConnectTimeout={int(self._connect_timeout)}",
            "-o", "StrictHostKeyChecking=accept-new",
            host.target,
            command,
        ]

    def run(self, name: str, command: str, timeout: float | None = None) -> dict[str, Any]:
        host = self.find(name)
        limit = min(timeout or self._timeout, self._timeout)
        start = time.monotonic()
        try:
            result = subprocess.run(self._args(host, command), capture_output=True, text=True, timeout=limit + self._connect_timeout)
        except subprocess.TimeoutExpired as exc:
            raise SshError(f"'{command}' on {host.name} didn't finish within {int(limit)}s") from exc
        except FileNotFoundError as exc:
            raise SshError(f"The '{self._ssh_binary}' client isn't installed where SirisAI runs") from exc
        if result.returncode == 255:
            # ssh's own failure (no route, refused, no key accepted), not the command's.
            raise SshError(f"Couldn't connect to {host.name} ({host.target}): {result.stderr.strip()[-300:] or 'ssh exited 255'}")
        return {
            "host": host.name,
            "command": command,
            "exit_code": result.returncode,
            "stdout": _clip(result.stdout),
            "stderr": _clip(result.stderr),
            "seconds": round(time.monotonic() - start, 2),
        }


def _clip(text: str) -> str:
    text = text or ""
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    return f"[first {len(text) - MAX_OUTPUT_CHARS} characters cut]\n" + text[-MAX_OUTPUT_CHARS:]


# One fixed command for a health snapshot, separated by markers so each part
# can be read back even when a tool is missing on that machine.
HEALTH_COMMAND = (
    "echo '##uptime'; cat /proc/loadavg 2>/dev/null; nproc 2>/dev/null; uptime -p 2>/dev/null; "
    "echo '##mem'; free -m 2>/dev/null; "
    "echo '##disk'; df -hP -x tmpfs -x devtmpfs -x overlay -x squashfs 2>/dev/null; "
    "echo '##failed'; systemctl --failed --no-legend --plain 2>/dev/null | head -20"
)


def parse_health(output: str) -> dict[str, Any]:
    sections: dict[str, list[str]] = {}
    current = None
    for line in output.splitlines():
        if line.startswith("##"):
            current = line[2:].strip()
            sections[current] = []
        elif current is not None and line.strip():
            sections[current].append(line)
    out: dict[str, Any] = {}
    up = sections.get("uptime", [])
    if up:
        parts = up[0].split()
        if len(parts) >= 3:
            out["load"] = [float(p) for p in parts[:3]]
        if len(up) > 1 and up[1].strip().isdigit():
            out["cpus"] = int(up[1])
        if len(up) > 2:
            out["up"] = up[2].removeprefix("up ").strip()
    for line in sections.get("mem", []):
        if line.lower().startswith("mem:"):
            fields = line.split()
            total, used = int(fields[1]), int(fields[2])
            available = int(fields[6]) if len(fields) > 6 else total - used
            out["memory"] = {"total_mb": total, "used_percent": round(100 * (total - available) / total) if total else None}
    disks = []
    for line in sections.get("disk", [])[1:]:
        fields = line.split()
        if len(fields) >= 6 and fields[4].endswith("%"):
            disks.append({"mount": fields[5], "size": fields[1], "used_percent": int(fields[4].rstrip("%")), "free": fields[3]})
    out["disks"] = disks
    out["failed_services"] = [line.split()[0] for line in sections.get("failed", []) if line.split()]
    warnings = []
    if "load" in out and out.get("cpus") and out["load"][1] > out["cpus"] * 1.5:
        warnings.append(f"load is high ({out['load'][1]} over 5 minutes on {out['cpus']} CPUs)")
    if (out.get("memory") or {}).get("used_percent") is not None and out["memory"]["used_percent"] >= 90:
        warnings.append(f"memory is {out['memory']['used_percent']}% used")
    warnings += [f"{d['mount']} is {d['used_percent']}% full ({d['free']} free)" for d in disks if d["used_percent"] >= 90]
    warnings += [f"{s} has failed" for s in out["failed_services"]]
    out["warnings"] = warnings
    return out
