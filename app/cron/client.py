"""SSH-based client for reading and toggling cron jobs on other machines on
the network (a home Linux server, Raspberry Pis) -- see app/routines/ for a
completely different concept, SirisAI's own scheduled tool runs. This is a
read/toggle window onto crontabs that already exist independently on other
machines.

Shells out to the system `ssh` + the remote `crontab` command rather than
pulling in a Python SSH library or installing an agent on those machines --
user-requested ("Hermes should be able to access both of these"): it picks
up whatever identity file/agent/known_hosts already grants SirisAI's own
host user access to reach them, the same way Hermes or a human already
would, instead of this codebase reinventing SSH key management.

Only each host's *own connecting-user* crontab (`crontab -l`/`crontab -`) is
read/written -- not /etc/crontab or /etc/cron.d/*, which would need root on
hosts this doesn't have it on and a riskier whole-file rewrite. Connecting
as root (one of the two hosts this was built for) already covers root's own
crontab, which is where most Pi automation typically lives anyway.
"""

import re
import subprocess
from dataclasses import dataclass
from typing import Any

_CRON_LINE_RE = re.compile(r"^(?P<schedule>@\w+|(?:\S+\s+){4}\S+)\s+(?P<command>.+)$")


@dataclass(frozen=True)
class CronHost:
    user: str
    host: str
    port: int = 22

    @property
    def label(self) -> str:
        return f"{self.user}@{self.host}"


def parse_cron_hosts(spec: str) -> list[CronHost]:
    """Parses SIRISAI_CRON_HOSTS, e.g. "brad@192.168.0.127,root@192.168.0.83"
    (optionally "user@host:port"), into CronHost entries. Blank entries
    (an empty spec, or a stray trailing comma) are skipped rather than
    producing a bogus host."""
    hosts = []
    for entry in spec.split(","):
        entry = entry.strip()
        if not entry:
            continue
        user, _, host_port = entry.partition("@")
        host, _, port = host_port.partition(":")
        hosts.append(CronHost(user=user, host=host, port=int(port) if port else 22))
    return hosts


class CronHostError(RuntimeError):
    """Raised when an SSH command to a cron host fails or times out."""


class CronHostsClient:
    def __init__(self, hosts: list[CronHost], timeout: float = 10.0, ssh_binary: str = "ssh") -> None:
        self._hosts = hosts
        self._timeout = timeout
        self._ssh_binary = ssh_binary

    @property
    def hosts(self) -> list[CronHost]:
        return list(self._hosts)

    def _ssh_args(self, host: CronHost, remote_command: str) -> list[str]:
        return [
            self._ssh_binary,
            "-p",
            str(host.port),
            "-o",
            "BatchMode=yes",
            "-o",
            f"ConnectTimeout={int(self._timeout)}",
            "-o",
            "StrictHostKeyChecking=accept-new",
            host.label,
            remote_command,
        ]

    def _run(self, host: CronHost, remote_command: str, input_text: str | None = None) -> str:
        try:
            result = subprocess.run(
                self._ssh_args(host, remote_command),
                input=input_text,
                capture_output=True,
                text=True,
                timeout=self._timeout + 5,
            )
        except subprocess.TimeoutExpired as exc:
            raise CronHostError(f"Timed out connecting to {host.label}") from exc
        except FileNotFoundError as exc:
            # Found live (2026-09-25): the container image had no `ssh` at
            # all, so this escaped as an unhandled FileNotFoundError and
            # GET /siris/cron_jobs returned a bare 500 instead of the
            # per-host error list_jobs() is built to produce.
            raise CronHostError(
                f"The '{self._ssh_binary}' client isn't installed where SirisAI runs, "
                f"so {host.label} can't be reached."
            ) from exc
        if result.returncode != 0:
            raise CronHostError(f"ssh {host.label} failed ({result.returncode}): {result.stderr.strip()}")
        return result.stdout

    def _find_host(self, host_label: str) -> CronHost:
        for host in self._hosts:
            if host.label == host_label:
                return host
        raise ValueError(f"Unknown/unconfigured cron host: {host_label}")

    def list_jobs(self) -> list[dict[str, Any]]:
        """Every parsed cron entry across all configured hosts. A host
        whose SSH connection fails contributes one {"host", "error"} entry
        instead of raising -- one unreachable Pi shouldn't blank the whole
        overview."""
        jobs: list[dict[str, Any]] = []
        for host in self._hosts:
            try:
                output = self._run(host, "crontab -l")
            except CronHostError as exc:
                # crontab's own way of saying "empty, not an error" is a
                # nonzero exit with this message on stderr -- distinct from
                # a real connection/permission failure, which is worth
                # surfacing to the user instead of silently showing "no jobs".
                if "no crontab for" in str(exc).lower():
                    continue
                jobs.append({"host": host.label, "error": str(exc)})
                continue
            jobs.extend(_parse_crontab(host.label, output))
        return jobs

    def set_job_enabled(self, host_label: str, line: str, enabled: bool) -> dict[str, Any]:
        """Comments out (disables) or uncomments (enables) one line in
        `host_label`'s own crontab, identified by its exact schedule+command
        text (the "line" field list_jobs() returns) -- not by position,
        since the crontab could have changed between listing and toggling.
        Rewrites the whole crontab via `crontab -` (the same mechanism
        `crontab -e` uses under the hood), so this only ever needs the
        access `crontab -l`/`crontab -e` already needs -- no sudo."""
        host = self._find_host(host_label)
        current = self._run(host, "crontab -l")
        lines = current.splitlines()
        target = line.strip()
        matched = False
        for i, raw in enumerate(lines):
            stripped = raw.strip()
            normalized = stripped.lstrip("#").strip() if stripped.startswith("#") else stripped
            if normalized == target:
                lines[i] = target if enabled else f"# {target}"
                matched = True
        if not matched:
            raise ValueError(f"No matching crontab line on {host_label}: {line!r}")
        new_content = "\n".join(lines) + "\n"
        self._run(host, "crontab -", input_text=new_content)
        return {"host": host_label, "line": target, "enabled": enabled}


def _parse_crontab(host_label: str, content: str) -> list[dict[str, Any]]:
    jobs = []
    for raw_line in content.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        enabled = not stripped.startswith("#")
        candidate = stripped if enabled else stripped.lstrip("#").strip()
        # Env var assignments (PATH=..., MAILTO=...) and plain comments
        # aren't schedule-shaped -- skip anything that doesn't parse as
        # <schedule> <command>, rather than misreporting them as jobs.
        match = _CRON_LINE_RE.match(candidate)
        if not match:
            continue
        jobs.append(
            {
                "host": host_label,
                "schedule": match.group("schedule"),
                "command": match.group("command"),
                "line": candidate,
                "enabled": enabled,
            }
        )
    return jobs


# -- the overview (5.73): every job with when it runs, what runs it and when it last ran --

OVERVIEW_COMMAND = (
    "echo '##info'; hostname 2>/dev/null; date +%z; uptime -p 2>/dev/null; "
    "echo '##user'; crontab -l 2>&1; "
    "echo '##file /etc/crontab'; cat /etc/crontab 2>/dev/null; "
    "for f in /etc/cron.d/*; do [ -f \"$f\" ] && { echo \"##file $f\"; cat \"$f\" 2>/dev/null; }; done; "
    "echo '##log'; { journalctl -q --no-pager -o short-iso -t CRON --since '-3 days' 2>/dev/null "
    "|| grep -h 'CRON\\[' /var/log/syslog /var/log/cron 2>/dev/null; } | grep 'CMD' | tail -n 600"
)
_SYSTEM_LINE_RE = re.compile(r"^(?P<schedule>@\w+|(?:\S+\s+){4}\S+)\s+(?P<user>[a-z_][\w.-]*)\s+(?P<command>.+)$")
_LOG_RE = re.compile(r"^(?P<at>\S+)\s+\S+\s+\S*CRON\[\d+\]:\s+\((?P<user>[^)]+)\)\s+CMD\s+\((?P<command>.*)\)\s*$")


def _sections(output: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    current = ""
    for line in output.splitlines():
        if line.startswith("##"):
            current = line[2:].strip()
            out.setdefault(current, [])
        elif current:
            out[current].append(line)
    return out


def _offset_minutes(text: str) -> int:
    m = re.fullmatch(r"([+-])(\d{2})(\d{2})", text.strip())
    if not m:
        return 0
    minutes = int(m.group(2)) * 60 + int(m.group(3))
    return -minutes if m.group(1) == "-" else minutes


def _last_runs(lines: list[str]) -> list[tuple[str, str, str]]:
    runs = []
    for line in lines:
        m = _LOG_RE.match(line.strip())
        if m:
            runs.append((m.group("at"), m.group("user"), m.group("command").strip()))
    return runs


def _last_run(runs: list[tuple[str, str, str]], command: str, user: str | None) -> str | None:
    want = command.strip()[:80]
    hits = [at for at, who, cmd in runs if cmd[:80] == want and (user is None or who == user)]
    return max(hits) if hits else None


def overview_for(host: CronHost, output: str, now: Any) -> dict[str, Any]:
    from app.cron.schedule import describe, next_run

    parts = _sections(output)
    info = parts.get("info", [])
    offset = _offset_minutes(info[1]) if len(info) > 1 else 0
    runs = _last_runs(parts.get("log", []))
    jobs: list[dict[str, Any]] = []

    def add(job: dict[str, Any], run_as: str | None) -> None:
        upcoming = next_run(job["schedule"], now, offset) if job["enabled"] else None
        jobs.append({
            **job,
            "description": describe(job["schedule"]),
            "next_run": upcoming.isoformat() if upcoming else None,
            "last_run": _last_run(runs, job["command"], run_as),
        })

    user_lines = parts.get("user", [])
    if not any("no crontab for" in line.lower() for line in user_lines):
        for job in _parse_crontab(host.label, "\n".join(user_lines)):
            add({**job, "source": "crontab", "run_as": host.user, "editable": True}, host.user)
    for key, lines in parts.items():
        if not key.startswith("file "):
            continue
        source = key[5:]
        for raw in lines:
            stripped = raw.strip()
            if not stripped or (stripped.startswith("#") and not _SYSTEM_LINE_RE.match(stripped.lstrip("#").strip())):
                continue
            enabled = not stripped.startswith("#")
            candidate = stripped if enabled else stripped.lstrip("#").strip()
            m = _SYSTEM_LINE_RE.match(candidate)
            if not m or "=" in m.group("schedule"):
                continue
            # run-parts lines are the cron.daily/weekly folders: name them as such.
            command = m.group("command")
            add({"host": host.label, "schedule": m.group("schedule"), "command": command, "line": candidate, "enabled": enabled,
                 "source": source, "run_as": m.group("user"), "editable": False}, m.group("user"))
    return {
        "host": host.label,
        "hostname": info[0].strip() if info else None,
        "utc_offset": info[1].strip() if len(info) > 1 else None,
        "uptime": info[2].strip() if len(info) > 2 else None,
        "reachable": True,
        "log_available": bool(parts.get("log")),
        "jobs": jobs,
    }


def overview(client: "CronHostsClient", now: Any = None) -> list[dict[str, Any]]:
    """Each host: its name, time offset and uptime, then every job (its own
    crontab, which can be switched on and off, and the system ones in
    /etc/crontab and /etc/cron.d, read only) with the schedule in words, the
    next run in the host's time and when it last ran (from the cron log,
    the last three days, when the account can read it)."""
    from datetime import UTC, datetime

    now = now or datetime.now(UTC)
    hosts = []
    for host in client.hosts:
        try:
            output = client._run(host, OVERVIEW_COMMAND)
        except CronHostError as exc:
            hosts.append({"host": host.label, "reachable": False, "error": str(exc), "jobs": []})
            continue
        hosts.append(overview_for(host, output, now))
    return hosts
