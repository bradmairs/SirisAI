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
from datetime import UTC, datetime
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
    "bills_hour_local": ("Life admin", "Check email for bills (hour)", "Blank: off", False),
    "things_email_hour_local": ("Life admin", "Check email for receipts (hour)", "Blank: off", False),
    "bills_warn_days": ("Life admin", "Warn about bills (days ahead)", "", False),
    "document_remind_days": ("Life admin", "Remind before a letter's due date (days)", "0: only on the day", False),
    "travel_hour_local": ("Life admin", "Trip checklist (hour)", "Blank: off", False),
    "travel_notice_days": ("Life admin", "Trip checklist (days before)", "", False),
    "travel_home_city": ("Life admin", "Home city", "Long events here aren't trips", False),
    "reliability_hour_local": ("Speed", "Weekly reliability report (hour, Sundays)", "Blank: off", False),
    "warranty_warn_days": ("Life admin", "Warn before a warranty ends (days)", "", False),
    "return_default_days": ("Life admin", "Return window when a receipt doesn't say (days)", "0: only when it says", False),
    "people_remind_days": ("Life admin", "Birthday reminders (days ahead)", "", False),
    "house_watch_seconds": ("Home", "Check for leaks and open doors every (seconds)", "0: off", False),
    "fridge_max_c": ("Home", "Fridge too warm above (°C)", "", False),
    "freezer_max_c": ("Home", "Freezer too warm above (°C)", "", False),
    "appliance_warm_minutes": ("Home", "Warm for this long before warning (minutes)", "", False),
    "door_open_minutes": ("Home", "Door or garage left open after (minutes)", "", False),
    "news_feeds": ("Morning brief", "News feeds", "RSS or Atom URLs, comma-separated", False),
    "news_topics": ("Morning brief", "News topics", "Comma-separated; blank: the top stories", False),
    "podcast_feeds": ("Morning brief", "Podcast feeds", "RSS URLs, comma-separated", False),
    "persona_style": ("Voice", "Personality", "jarvis (brief, dry British wit) or plain", False),
    "persona": ("Voice", "Extra personality instructions", "Added after the personality, e.g. 'Call me Brad'", False),
    "tts_voice": ("Voice", "Speaking voice", "Kokoro British male voices: bm_george, bm_lewis, bm_daniel, bm_fable", False),
    "tts_speed": ("Voice", "Speaking speed", "1.0 natural; blank: the personality's (1.2 for jarvis)", False),
    "voice_roles": ("Voice", "Voice roles", "user=adult|child|guest, comma-separated", False),
    "voice_unknown_as_guest": ("Voice", "Treat unrecognised voices as guests", "No personal info, no approvals", False),
    "quick_commands": ("Speed", "Instant home commands", "Plain light/switch/fan commands without the model", False),
    "ollama_keep_alive": ("Speed", "Keep the model loaded for", "e.g. 30m; -1 always; blank: Ollama's 5 minutes", True),
    "ollama_warm_hours": ("Speed", "Keep the model warm (hours)", "start-end, local, e.g. 6-23; blank: never", False),
    "ssh_hosts": ("Servers", "Servers", "name=user@host, comma-separated (blank: from ~/.ssh/config)", True),
    "cron_hosts": ("Servers", "Cron servers", "user@host[:port], comma-separated; add them from the Cron tab", False),
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
    "server_backups": ("Servers", "Server backups to check", "server:zfs:dataset or server:path:/folder, comma-separated", True),
    "server_backup_max_age_hours": ("Servers", "A backup is overdue after (hours)", "", False),
    "server_backups_hour_local": ("Servers", "Backup check (hour)", "Blank: off. Test-read on the 1st of the month", False),
    "container_updates_hour_local": ("Homelab", "Container update check (hour)", "Blank: off", False),
    "container_update_exclude": ("Homelab", "Containers never to update", "Comma-separated names", False),
    "container_update_health_seconds": ("Homelab", "Roll back an update not healthy within (seconds)", "", False),
    "network_device_watch_minutes": ("Homelab", "Look for new network devices every (minutes)", "0: off. Needs UniFi", False),
    "internet_probe_seconds": ("Homelab", "Check the internet every (seconds)", "0: off", False),
    "internet_probe_targets": ("Homelab", "Internet check targets", "host:port, comma-separated; down when none answer", False),
    "internet_speedtest_hours": ("Homelab", "Speed test every (hours)", "0: off", False),
    "internet_report_hour_local": ("Homelab", "Weekly internet summary, Mondays (hour)", "Blank: off", False),
    "security_cert_hosts": ("Homelab", "Certificates to watch", "host or host:port, comma-separated", False),
    "security_domains": ("Homelab", "Domains to watch", "Comma-separated", False),
    "expiry_check_servers": ("Homelab", "Also check HTTPS on each server", "", False),
    "expiry_check_hour_local": ("Homelab", "Certificate and domain check (hour)", "Blank: off", False),
    "security_cert_warn_days": ("Homelab", "Warn this many days before expiry", "", False),
    "away_mode": ("Home", "Away mode", "ask, auto or off: when everyone leaves, offer or run the away protocol", False),
    "away_protocol": ("Home", "Away protocol", "The protocol to run when everyone's out", False),
    "camera_digest_hours": ("Home", "Camera digest at (hours)", "Comma-separated local hours; blank: off", False),
    "camera_digest_push": ("Home", "Send the camera digest to my phone", "", False),
    "energy_report_hour_local": ("Home", "Weekly energy report, Mondays (hour)", "Blank: off", False),
    "energy_flat_rate": ("Home", "Electricity price per kWh", "Used when there's no price sensor; 0: no cost", False),
    "energy_overnight_watts": ("Home", "Left on overnight means more than (W)", "", False),
    "energy_ignore": ("Home", "Leave these out of the energy report", "Entity ids or names, comma-separated", False),
    "audit_digest_hour_local": ("Approvals", "Weekly audit digest, Sundays (hour)", "Blank: off", False),
    "health_weight_goal_kg": ("Health", "Goal weight (kg)", "Blank: none", False),
    "health_stale_hours": ("Health", "Say the import has stopped after (hours)", "0: never", False),
    "health_private": ("Health", "Keep health answers off cloud models", "A local model or SirisAI's own words answer instead", False),
    "health_check_hour_local": ("Health", "Getting-sick check (hour)", "Blank: off", False),
    "health_wind_down": ("Health", "Wind-down before bed", "ask, auto or off", False),
    "health_wind_down_minutes": ("Health", "Wind down this long before bedtime (minutes)", "", False),
    "health_morning_buffer_minutes": ("Health", "Wake this long before the first event (minutes)", "", False),
    "health_bedroom_temperature_entity": ("Health", "Bedroom temperature sensor", "Home Assistant entity, for sleep insights", False),
    "health_screen_entity": ("Health", "TV or screen", "Home Assistant entity, for sleep insights", False),
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


HISTORY_KEY = "settings_history"
HISTORY_KEEP = 50


def history(siris: "SirisAI") -> list[dict[str, Any]]:
    """Changes made here, newest first: when, each setting's value before and
    after, and whether it was set here before (so an undo can put it back to
    .env or the default rather than pinning that value)."""
    return list(siris.state.get(HISTORY_KEY) or [])


def undo(siris: "SirisAI", change_id: int) -> dict[str, Any]:
    """Put back what one change replaced (a new change, itself undoable)."""
    entry = next((h for h in history(siris) if h["id"] == change_id), None)
    if entry is None:
        raise LookupError(f"No settings change {change_id}")
    # None clears the override; a value that was set here *to* blank goes back as "" (blank = off).
    restore = {name: (("" if c["before"] is None else c["before"]) if c["was_set_here"] else None) for name, c in entry["changes"].items()}
    return update(siris, restore, undo_of=change_id)


def _apply_live(siris: "SirisAI", changes: dict[str, Any]) -> None:
    """6.7: the voice and its pace change on the next thing said, no restart."""
    if not {"tts_voice", "tts_speed", "persona_style"} & set(changes):
        return
    from app.cortex import persona

    tts = getattr(siris, "tts", None)
    inner = getattr(tts, "inner", tts)  # through the effects wrapper
    for client in {id(tts): tts, id(inner): inner}.values():
        if client is None:
            continue
        if hasattr(client, "speed"):
            client.speed = persona.tts_speed(siris.settings)
        if hasattr(client, "voice"):
            client.voice = siris.settings.tts_voice


def update(siris: "SirisAI", changes: dict[str, Any], undo_of: int | None = None) -> dict[str, Any]:
    """Validate, store and apply. Raises ValueError for an unknown field or a
    bad value (nothing is stored then). A value of None clears the web
    override, going back to .env or the default."""
    from pydantic import ValidationError

    from app.core.config import Settings

    unknown = sorted(set(changes) - set(EDITABLE))
    if unknown:
        raise ValueError(f"These can't be changed here: {', '.join(unknown)}")
    stored = load()
    before = {name: {"before": getattr(siris.settings, name), "was_set_here": name in stored} for name in changes}
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
    _apply_live(siris, changes)
    siris.audit.record("settings_update", {"changed": sorted(changes)}, result={"applied": applied, "restart": restart}, trigger="the Settings page")
    past = history(siris)
    entry = {
        "id": (past[0]["id"] + 1) if past else 1,
        "at": datetime.now(UTC).isoformat(),
        "changes": {name: {**before[name], "after": getattr(fresh, name)} for name in changes},
        "undo_of": undo_of,
    }
    siris.state.set(HISTORY_KEY, json.loads(json.dumps([entry, *past][:HISTORY_KEEP], default=str)))
    if undo_of is None:
        from app.core import undo

        undo.add(siris, "changed " + ", ".join(sorted(changes)).replace("_", " "), [{"setting_change": entry["id"]}])
    return {"applied": applied, "needs_restart": restart, "change": entry}
