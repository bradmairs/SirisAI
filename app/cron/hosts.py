"""Adding and removing cron servers from the web UI (5.73).

`add(siris, "brad@192.168.0.127")` checks the address, tries to reach it
over SSH (the same ambient identity everything else uses; no key setting),
then saves SIRISAI_CRON_HOSTS through the Settings page store so it
survives a restart, and swaps in a new cron client straight away. When the
server turns SirisAI away (no key there yet), the error says so and gives
SirisAI's own public key to add to that server's ~/.ssh/authorized_keys.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.cron.client import CronHost, CronHostError, CronHostsClient, parse_cron_hosts

if TYPE_CHECKING:
    from app.core.siris import SirisAI

TARGET_RE = re.compile(r"^[a-z_][a-z0-9_.-]{0,31}@[A-Za-z0-9][A-Za-z0-9.-]{0,252}(?::\d{1,5})?$")
_AUTH_HINTS = ("permission denied", "publickey", "authentication")


def public_key() -> str | None:
    """SirisAI's own SSH public key (the public half only), if it has one."""
    ssh_dir = Path.home() / ".ssh"
    for name in ("id_ed25519.pub", "id_ecdsa.pub", "id_rsa.pub"):
        try:
            text = (ssh_dir / name).read_text().strip()
        except OSError:
            continue
        if text:
            return text
    return None


def _spec(hosts: list[CronHost]) -> str:
    return ",".join(h.label + (f":{h.port}" if h.port != 22 else "") for h in hosts)


def apply(siris: "SirisAI", hosts: list[CronHost]) -> None:
    """Use these hosts from now on, registering the cron tools the first time."""
    from app.plugins.builtin.cron import CronListJobsTool, CronSetJobEnabledTool

    if not hosts:
        siris.cron_client = None
        return
    siris.cron_client = CronHostsClient(hosts)
    for tool_cls in (CronListJobsTool, CronSetJobEnabledTool):
        existing = siris.tools.get(tool_cls.name)
        if existing is None:
            siris.tools.register(tool_cls(siris.cron_client))
        else:
            existing._client = siris.cron_client  # type: ignore[attr-defined]


def add(siris: "SirisAI", target: str, check: bool = True) -> dict[str, Any]:
    from app.core import settings_overrides

    target = target.strip()
    if not TARGET_RE.match(target):
        raise ValueError("Give the server as user@host, or user@host:port, e.g. brad@192.168.0.127.")
    new = parse_cron_hosts(target)[0]
    if not 0 < new.port < 65536:
        raise ValueError("The port must be between 1 and 65535.")
    current = parse_cron_hosts(siris.settings.cron_hosts)
    if any(h.label == new.label for h in current):
        raise ValueError(f"{new.label} is already in the list.")
    info: dict[str, Any] = {}
    if check:
        try:
            out = CronHostsClient([new])._run(new, "hostname; crontab -l 2>&1 | grep -vc '^#'")
        except CronHostError as exc:
            message = str(exc)
            if any(h in message.lower() for h in _AUTH_HINTS):
                key = public_key()
                message += (
                    f" -- {new.host} doesn't accept SirisAI's key yet. Add this line to ~{new.user}/.ssh/authorized_keys there: {key}"
                    if key else f" -- {new.host} doesn't accept SirisAI's SSH key, and SirisAI has none yet (run ssh-keygen where it runs)."
                )
            raise ConnectionError(message) from None
        lines = out.split()
        info = {"hostname": lines[0] if lines else None}
    settings_overrides.update(siris, {"cron_hosts": _spec([*current, new])})
    apply(siris, parse_cron_hosts(siris.settings.cron_hosts))
    return {"added": new.label, **info, "hosts": [h.label for h in parse_cron_hosts(siris.settings.cron_hosts)]}


def remove(siris: "SirisAI", label: str) -> dict[str, Any]:
    from app.core import settings_overrides

    current = parse_cron_hosts(siris.settings.cron_hosts)
    kept = [h for h in current if h.label != label]
    if len(kept) == len(current):
        raise LookupError(f"{label} isn't one of the cron servers.")
    settings_overrides.update(siris, {"cron_hosts": _spec(kept)})
    apply(siris, kept)
    return {"removed": label, "hosts": [h.label for h in kept]}
