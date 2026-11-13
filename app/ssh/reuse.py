"""One SSH connection per server, reused for a while (6.38).

Every check (health, updates, disks, the cron overview, the reboot
follow-up) used to open a fresh SSH connection: a key exchange and login
each time, often most of a second. With SIRISAI_SSH_REUSE_SECONDS (120;
0 turns it off) the first command to a server starts a shared connection
in the background (`ssh -fNM`, OpenSSH's ControlMaster, its output to
/dev/null so nothing waits on it), and later commands go through it.

Commands themselves run with ControlMaster=no: when the shared connection
is there they use it, when it isn't (it timed out, the server rebooted)
they simply connect as before. So reuse can make things faster but never
breaks a command.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import threading
import time
from pathlib import Path

_lock = threading.Lock()
_started: dict[str, float] = {}


def control_dir() -> Path:
    path = Path(os.environ.get("SIRISAI_SSH_CONTROL_DIR") or Path(tempfile.gettempdir()) / f"sirisai-ssh-{os.getuid()}")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def options(seconds: float) -> list[str]:
    """The options for a command: use the shared connection if there is one."""
    if seconds <= 0:
        return []
    return ["-o", "ControlMaster=no", "-o", f"ControlPath={control_dir()}/%C"]


def ensure(base: list[str], target: str, seconds: float, connect_timeout: float) -> None:
    """Start the shared connection to `target` unless one is running.
    `base` is the ssh binary and its options, without the target or command."""
    if seconds <= 0:
        return
    key = " ".join([*base, target])
    now = time.monotonic()
    with _lock:
        if now - _started.get(key, -1e9) < seconds / 2:
            return
        _started[key] = now
    path = ["-o", f"ControlPath={control_dir()}/%C"]
    try:
        alive = subprocess.run([*base, *path, "-O", "check", target], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=5).returncode == 0
        if not alive:
            subprocess.run([*base, *path, "-o", "ControlMaster=yes", "-o", f"ControlPersist={int(seconds)}", "-f", "-N", target],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=connect_timeout + 5)
    except (OSError, subprocess.SubprocessError):
        with _lock:
            _started.pop(key, None)  # try again next time; the command connects on its own meanwhile


def forget() -> None:
    with _lock:
        _started.clear()
