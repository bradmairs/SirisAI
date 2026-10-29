"""Settings changed from the web UI's Settings page.

They're kept in a small JSON file (SIRISAI_OVERRIDES_FILE, default
./data/settings-overrides.json, next to the database) and win over .env and
the environment, so a change survives a restart. Only the fields in
EDITABLE can be changed this way. No secrets are among them: API keys,
passwords and the approval PIN stay in .env.

A change applies straight away to the running SirisAI where the setting is
read each time it's used (almost all of them). The few marked
`restart: True` are read once at start-up and say so.
"""

import json
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.core.config import Settings
    from app.core.siris import SirisAI

logger = logging.getLogger(__name__)

# name -> (group, label, help, needs a restart)
EDITABLE: dict[str, tuple[str, str, str, bool]] = {
    "ssh_hosts": ("Servers", "Servers", "name=user@host, comma-separated (blank: from ~/.ssh/config)", True),
    "ssh_allow_changes": ("Servers", "Allow changes over SSH", "Commands that change things, always asking first", False),
    "ssh_allow_power": ("Servers", "Allow reboot and shutdown", "Always asking first", True),
    "ssh_safe_commands": ("Servers", "Extra look-only commands", "Comma-separated command names that never ask", False),
    "ssh_command_timeout_seconds": ("Servers", "Command timeout (seconds)", "", True),
    "ssh_wake_macs": ("Servers", "Wake-on-LAN computers", "name=MAC or name=MAC@address, comma-separated; servers' MACs are learned", False),
    "ssh_wake_relay": ("Servers", "Wake-on-LAN relay server", "A server on the LAN that sends the magic packet", False),
    "ssh_wake_broadcast": ("Servers", "Wake-on-LAN broadcast address", "Blank: worked out from each server's address", False),
    "ssh_wake_timeout_seconds": ("Servers", "Wait for a woken server (seconds)", "0: don't wait", False),
    "server_watch_minutes": ("Servers", "Server watch every (minutes)", "0: off", False),
    "server_disk_warn_percent": ("Servers", "Warn when a disk is this full (%)", "", False),
    "server_updates_hour_local": ("Servers", "OS update check (hour)", "Blank: off", False),
    "server_disks_hour_local": ("Servers", "Disk health check (hour)", "Blank: off", False),
    "approval_expiry_minutes": ("Approvals", "Approvals expire after (minutes)", "0: never", False),
    "approval_pin_tools": ("Approvals", "More tools that need the PIN", "Comma-separated tool names", False),
    "briefing_hour_local": ("Daily", "Morning briefing (hour)", "Blank: off", False),
    "triage_hour_local": ("Daily", "Morning triage (hour)", "Blank: off", False),
    "backup_hour_local": ("Daily", "Backup (hour)", "Blank: off", False),
    "housekeeping_hour_local": ("Daily", "Housekeeping (hour)", "Blank: off", False),
    "update_check_hour_local": ("Daily", "SirisAI update check (hour)", "Blank: off", False),
    "brain_upkeep_hour_local": ("Daily", "Second brain upkeep (hour)", "Blank: off", False),
    "reflection_hour_local": ("Daily", "Reflection (hour)", "Blank: off", False),
}


def path() -> Path:
    return Path(os.environ.get("SIRISAI_OVERRIDES_FILE", "./data/settings-overrides.json"))


def load() -> dict[str, Any]:
    try:
        data = json.loads(path().read_text())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        logger.exception("Couldn't read %s; ignoring it", path())
        return {}
    return {k: v for k, v in data.items() if k in EDITABLE} if isinstance(data, dict) else {}


def _kind(annotation: Any) -> str:
    text = str(annotation)
    if annotation is bool:
        return "bool"
    if annotation in (int, float) or "int" in text or "float" in text:
        return "number"
    return "text"


def describe(settings: "Settings") -> list[dict[str, Any]]:
    from app.core.config import Settings

    stored = load()
    fields = Settings.model_fields
    out = []
    for name, (group, label, help_text, restart) in EDITABLE.items():
        if name not in fields:
            continue
        out.append(
            {
                "name": name,
                "group": group,
                "label": label,
                "help": help_text,
                "type": _kind(fields[name].annotation),
                "optional": "None" in str(fields[name].annotation),
                "value": getattr(settings, name),
                "default": fields[name].default,
                "source": "web" if name in stored else "default/.env",
                "restart": restart,
            }
        )
    return out


def update(siris: "SirisAI", changes: dict[str, Any]) -> dict[str, Any]:
    """Validate, store and apply. Raises ValueError for an unknown field or a
    bad value (nothing is stored then). A value of None clears the web
    override, going back to .env or the default."""
    from pydantic import ValidationError

    from app.core.config import Settings

    unknown = sorted(set(changes) - set(EDITABLE))
    if unknown:
        raise ValueError(f"These can't be changed here: {', '.join(unknown)}")
    stored = load()
    for name, value in changes.items():
        if value is None:
            stored.pop(name, None)
        else:
            stored[name] = value
    try:
        fresh = Settings(**stored)
    except ValidationError as exc:
        problems = "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors())
        raise ValueError(problems) from None
    target = path()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps({k: getattr(fresh, k) for k in stored}, indent=2, default=str))
    tmp.replace(target)
    applied, restart = [], []
    for name in changes:
        setattr(siris.settings, name, getattr(fresh, name))
        (restart if EDITABLE[name][3] else applied).append(name)
    siris.audit.record("settings_update", {"changed": sorted(changes)}, result={"applied": applied, "restart": restart}, trigger="the Settings page")
    return {"applied": applied, "needs_restart": restart}
