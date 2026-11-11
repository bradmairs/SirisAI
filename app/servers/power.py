"""Following up a reboot or shutdown (5.6).

ssh_power used to stop at "Rebooting the NAS", even when the reboot never
happened. Now each one it starts is kept here with the server's boot id
from just before, and checked every 30 seconds:

- a reboot is done when the server answers with a *new* boot id: "The NAS
  rebooted and is back (1m 40s)". Still the old boot id after 3 minutes
  means it never went down: an alert says so. Not answering after 10
  minutes: an alert that it hasn't come back.
- a shutdown is done once the server stops answering. Still up after 3
  minutes: an alert.
- a delayed one waits for its time first.

Kept in siris.state, so it carries on after SirisAI's own server reboots
(that one's always done last).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from app.ssh.client import BOOT_ID_COMMAND, SshError

if TYPE_CHECKING:
    from app.core.siris import SirisAI

logger = logging.getLogger(__name__)
STATE_KEY = "power:pending"
STILL_UP_MINUTES = 3
BACK_WITHIN_MINUTES = 10


def track(siris: "SirisAI", server: str, action: str, boot_id: str | None, delay_minutes: int = 0, now: datetime | None = None) -> None:
    now = now or datetime.now(UTC)
    pending = [p for p in (siris.state.get(STATE_KEY) or []) if p.get("server") != server]
    pending.append({
        "server": server, "action": action, "boot_id": boot_id,
        "due": (now + timedelta(minutes=delay_minutes)).isoformat(), "seen_down": False,
    })
    siris.state.set(STATE_KEY, pending)


def pending(siris: "SirisAI") -> list[dict[str, Any]]:
    return list(siris.state.get(STATE_KEY) or [])


def _boot_id(siris: "SirisAI", server: str) -> str | None:
    """The current boot id, or None when the server doesn't answer."""
    try:
        out = siris.ssh_client.run(server, BOOT_ID_COMMAND, timeout=10)  # type: ignore[union-attr]
    except (SshError, ValueError):
        return None
    return out["stdout"].strip() or "unknown"


def _took(seconds: float) -> str:
    minutes, secs = divmod(int(seconds), 60)
    return f"{minutes}m {secs}s" if minutes else f"{secs}s"


def check(siris: "SirisAI", now: datetime | None = None) -> list[dict[str, Any]]:
    """Check each pending reboot or shutdown; returns the ones finished now."""
    items = pending(siris)
    if not items or siris.ssh_client is None:
        return []
    now = now or datetime.now(UTC)
    keep, finished = [], []
    for item in items:
        server, action = item["server"], item["action"]
        due = datetime.fromisoformat(item["due"])
        if now < due:
            keep.append(item)
            continue
        waited = (now - due).total_seconds()
        current = _boot_id(siris, server)
        key = f"power:{server}"
        if current is None:
            item["seen_down"] = True
        if action == "reboot":
            if current is not None and item.get("boot_id") and current not in (item["boot_id"], "unknown"):
                siris.attention.raise_item(key, "notice", f"{server} rebooted and is back ({_took(waited)})",
                                           source="servers-power", severity="info")
                finished.append({**item, "outcome": "back"})
                continue
            if current is not None and current == item.get("boot_id") and not item["seen_down"] and waited >= STILL_UP_MINUTES * 60:
                siris.attention.raise_item(
                    key, "alert", f"{server} didn't reboot", source="servers-power",
                    body="It took the reboot command but is still running since before it. Check that "
                         "`sudo -n systemctl reboot` works for SirisAI's account there (deploy/README.md: SSH to your other servers).",
                )
                finished.append({**item, "outcome": "not rebooted"})
                continue
            if current is not None and not item.get("boot_id") and item["seen_down"]:
                siris.attention.raise_item(key, "notice", f"{server} rebooted and is back ({_took(waited)})",
                                           source="servers-power", severity="info")
                finished.append({**item, "outcome": "back"})
                continue
            if waited >= BACK_WITHIN_MINUTES * 60:
                siris.attention.raise_item(
                    key, "alert", f"{server} hasn't come back after rebooting", source="servers-power",
                    body=f"No answer over SSH for {_took(waited)}. It may be stuck at boot (a disk check, a prompt) or "
                         "need power cycling.",
                    actions=[{"id": "wake", "label": "Try Wake-on-LAN"}] if siris.tools.get("ssh_wake") else None,
                    payload={"server": server},
                )
                finished.append({**item, "outcome": "not back"})
                continue
        else:  # shutdown
            if current is None:
                siris.attention.raise_item(key, "notice", f"{server} is off", source="servers-power", severity="info")
                finished.append({**item, "outcome": "off"})
                continue
            if waited >= STILL_UP_MINUTES * 60:
                siris.attention.raise_item(
                    key, "alert", f"{server} didn't shut down", source="servers-power",
                    body="It's still answering. Check that `sudo -n systemctl poweroff` works for SirisAI's account there.",
                )
                finished.append({**item, "outcome": "still up"})
                continue
        keep.append(item)
    siris.state.set(STATE_KEY, keep)
    return finished
