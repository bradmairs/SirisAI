"""The remote commands behind the server checks (app/servers/watch.py), and
their parsers: what OS updates are waiting, whether a reboot is needed, and
how the disks are (SMART, ZFS, software RAID).

Everything here only reads, except `install_updates_command`, which the user
approves first. A non-root account needs passwordless sudo for the parts
that need root (SMART, installing updates); without it, those parts report
what's missing instead of failing the whole check.
"""

import re
from typing import Any

from app.ssh.client import NEEDS_SUDO_EXIT, SshHost, sudo_check, sudo_prefix

# Exactly what runs, so the sudo check asks about the same command a sudoers
# rule allows ("/usr/bin/apt-get -y -q *" covers both apt ones).
APT_UPDATE = "apt-get -y -q update"
APT_UPGRADE = "apt-get -y -q -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold upgrade"
DNF_UPGRADE = "dnf -y -q upgrade"
UPDATE_LOG = "/tmp/sirisai-updates.log"
UPDATE_DONE = "/tmp/sirisai-updates.done"
_COUNTERS = {
    "reallocated_sector": "reallocated sectors",
    "current_pending": "sectors waiting to be reallocated",
    "offline_uncorrectable": "uncorrectable sectors",
    "media and data integrity": "media errors",
}
_UNIT_RE = re.compile(r"^[A-Za-z0-9@._:\-]+$")


def updates_command() -> str:
    return (
        "echo '##pm'; "
        "if command -v apt >/dev/null 2>&1; then echo apt; apt list --upgradable 2>/dev/null | tail -n +2; "
        "elif command -v dnf >/dev/null 2>&1; then echo dnf; dnf -q check-update 2>/dev/null; "
        "else echo none; fi; "
        "echo '##reboot'; "
        "if [ -f /var/run/reboot-required ]; then echo yes; cat /var/run/reboot-required.pkgs 2>/dev/null | sort -u | head -10; "
        "elif command -v needs-restarting >/dev/null 2>&1 && ! needs-restarting -r >/dev/null 2>&1; then echo yes; fi; "
        f"echo '##last'; [ -f {UPDATE_DONE} ] && cat {UPDATE_DONE} && tail -n 5 {UPDATE_LOG} 2>/dev/null"
    )


def parse_updates(output: str) -> dict[str, Any]:
    sections = _sections(output)
    pm = (sections.get("pm") or ["none"])[0].strip()
    lines = sections.get("pm", [])[1:]
    packages = []
    security = 0
    for line in lines:
        if pm == "apt" and "/" in line:
            name, _, rest = line.partition("/")
            suite = rest.split()[0] if rest.split() else ""
            is_security = "-security" in suite
            security += is_security
            packages.append({"name": name.strip(), "security": is_security})
        elif pm == "dnf" and len(line.split()) >= 3 and not line.startswith(("Obsoleting", "Last metadata")):
            packages.append({"name": line.split()[0].rsplit(".", 1)[0], "security": False})
    reboot = sections.get("reboot", [])
    last = sections.get("last", [])
    out: dict[str, Any] = {
        "package_manager": pm,
        "updates": len(packages),
        "security": security,
        "packages": [p["name"] for p in packages][:30],
        "reboot_required": bool(reboot and reboot[0].strip() == "yes"),
        "reboot_because": [r.strip() for r in reboot[1:]],
    }
    if last:
        out["last_install"] = {"exit_code": int(last[0]) if last[0].strip().lstrip("-").isdigit() else None, "log_tail": "\n".join(last[1:])[-600:]}
    return out


def install_updates_command(host: SshHost, package_manager: str) -> str:
    """Install every waiting update, in the background (it can take minutes),
    logging to /tmp so the next check can say how it went."""
    sudo = sudo_prefix(host)
    if package_manager == "apt":
        check = sudo_check(host, APT_UPDATE) + (sudo_check(host, APT_UPGRADE) if sudo else "")
        work = f"{sudo}{APT_UPDATE} && {sudo}{APT_UPGRADE}"
    elif package_manager == "dnf":
        check = sudo_check(host, DNF_UPGRADE)
        work = f"{sudo}{DNF_UPGRADE}"
    else:
        raise ValueError(f"Don't know how to install updates with '{package_manager}' (apt and dnf are supported).")
    return (
        check
        + f"rm -f {UPDATE_DONE}; nohup sh -c '{work} </dev/null >{UPDATE_LOG} 2>&1; echo $? >{UPDATE_DONE}' >/dev/null 2>&1 & echo started"
    )


def restart_unit_command(host: SshHost, unit: str) -> str:
    if not _UNIT_RE.fullmatch(unit or ""):
        raise ValueError(f"'{unit}' doesn't look like a systemd unit name.")
    return sudo_check(host, f"systemctl restart {unit}") + f"{sudo_prefix(host)}systemctl restart {unit} && echo restarted"


def disks_command(host: SshHost) -> str:
    sudo = sudo_prefix(host)
    smart_loop = (
        "for d in $(lsblk -dno NAME,TYPE 2>/dev/null | awk '$2==\"disk\" && $1 !~ /^(zram|loop|sr|ram)/ {print $1}'); do "
        f"echo \"@@$d\"; {sudo}smartctl -H -A /dev/$d 2>&1 | grep -Ei "
        "'overall-health|SMART Health Status|Reallocated_Sector_Ct|Current_Pending_Sector|Offline_Uncorrectable|"
        "Percentage Used|Media and Data Integrity Errors|Unable to detect|command not found'; done"
    )
    if sudo:
        smart = f"if ! command -v smartctl >/dev/null 2>&1; then echo NOSMARTCTL; elif sudo -n -l smartctl >/dev/null 2>&1; then {smart_loop}; else echo NOSUDO; fi"
    else:
        smart = f"if command -v smartctl >/dev/null 2>&1; then {smart_loop}; else echo NOSMARTCTL; fi"
    return f"echo '##smart'; {smart}; echo '##zpool'; zpool status -x 2>/dev/null; echo '##md'; cat /proc/mdstat 2>/dev/null"


def parse_disks(output: str) -> dict[str, Any]:
    sections = _sections(output)
    smart_lines = sections.get("smart", [])
    disks: list[dict[str, Any]] = []
    problems: list[str] = []
    note = None
    if smart_lines[:1] == ["NOSUDO"]:
        note = "SMART needs passwordless sudo for smartctl"
    elif smart_lines[:1] == ["NOSMARTCTL"]:
        note = "smartctl isn't installed (smartmontools)"
    else:
        current: dict[str, Any] | None = None
        for line in smart_lines:
            if line.startswith("@@"):
                current = {"disk": line[2:].strip(), "health": None, "issues": []}
                disks.append(current)
                continue
            if current is None:
                continue
            low = line.lower()
            if "overall-health" in low or "smart health status" in low:
                current["health"] = line.split(":", 1)[-1].strip()
                if not re.search(r"passed|\bok\b", low):
                    current["issues"].append(f"SMART health {current['health']}")
            elif "unable to detect" in low:
                current["health"] = "unknown"
            else:
                value = _raw_value(line)
                if value is None:
                    continue
                if "percentage used" in low and value >= 90:
                    current["issues"].append(f"{value}% of its rated life used")
                elif "percentage used" not in low and value > 0:
                    label = next((plain for raw, plain in _COUNTERS.items() if raw in low), "errors")
                    current["issues"].append(f"{value} {label}")
        for d in disks:
            if d["issues"]:
                problems.append(f"{d['disk']}: {', '.join(d['issues'])}")
    zpool = [line.strip() for line in sections.get("zpool", []) if line.strip()]
    pools_healthy = not zpool or zpool[0] == "all pools are healthy"
    if not pools_healthy:
        state = next((line for line in zpool if line.startswith("state:")), "state: unhealthy")
        pool = next((line.split(":", 1)[1].strip() for line in zpool if line.startswith("pool:")), "a ZFS pool")
        problems.append(f"ZFS pool {pool} is {state.split(':', 1)[1].strip()}")
    array = "array"
    for line in sections.get("md", []):
        match = re.match(r"^(md\d+)\s*:", line)
        if match:
            array = match.group(1)
        status = re.search(r"\[([U_]+)\]", line)
        if status and "_" in status.group(1):
            problems.append(f"RAID {array} is degraded ({status.group(1)})")
    return {"disks": disks, "smart_note": note, "zfs_healthy": pools_healthy, "problems": problems}


def _raw_value(line: str) -> int | None:
    if ":" in line:  # NVMe: "Percentage Used: 3%", "Media and Data Integrity Errors: 0"
        tail = line.split(":", 1)[1].strip().rstrip("%").replace(",", "")
        return int(tail) if tail.isdigit() else None
    fields = line.split()  # ATA attribute table: the raw value is the 10th column
    return int(fields[9]) if len(fields) >= 10 and fields[9].isdigit() else None


def _sections(output: str) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {}
    current = None
    for line in output.splitlines():
        if line.startswith("##"):
            current = line[2:].strip()
            sections[current] = []
        elif current is not None and line.strip():
            sections[current].append(line.rstrip())
    return sections


def needs_sudo(result: dict[str, Any]) -> bool:
    return result.get("exit_code") == NEEDS_SUDO_EXIT
