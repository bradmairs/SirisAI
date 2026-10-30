"""A weekly audit digest (Sundays at SIRISAI_AUDIT_DIGEST_HOUR_LOCAL, 6pm):
what SirisAI did on its own, what it held for your OK, what it was refused,
wrong approval PINs, and settings changed, in plain words in the feed.

    SirisAI's week: 34 things done on its own (21 notifications, 6 device
    changes, 3 container restarts), 2 held for your OK, 1 refused. No wrong
    PINs.

"On its own" is the same classification as the autonomy timeline
(app/audit/autonomy.py); reads are left out. `audit_digest` gives the same
for any number of days.
"""

from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from app.audit.autonomy import is_autonomous, timeline
from app.core.approvals import LOCK_AFTER

if TYPE_CHECKING:
    from app.core.siris import SirisAI

NOUNS = {
    "notify": ("notification", "notifications"),
    "home_assistant_call_service": ("device change", "device changes"),
    "docker_restart_container": ("container restart", "container restarts"),
    "speak_in_current_room": ("announcement", "announcements"),
    "remember": ("memory kept", "memories kept"),
    "remember_entity_fact": ("fact learned", "facts learned"),
    "run_protocol": ("protocol run", "protocol runs"),
}
REFUSED_MARKERS = ("pin_required", "restricted", "not enabled", "turned off", "not allowed", "refused", "guest mode")


def _plural(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


def digest(siris: "SirisAI", days: float = 7, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    entries = siris.audit.since(now - timedelta(days=days), limit=5000)
    done = [i for i in timeline(siris, hours=days * 24, now=now)["items"] if i["outcome"] == "done"]
    by_tool = Counter(i["tool"] for i in done)
    held = [e for e in entries if e.get("confirmation_required")]
    refused = [
        e for e in entries
        if e.get("error") and e["tool_name"] != "approval_pin" and any(m in e["error"].lower() for m in REFUSED_MARKERS)
    ]
    wrong_pins = [e for e in entries if e["tool_name"] == "approval_pin" and e.get("error")]
    lockouts = sum(1 for e in wrong_pins if _count(e) == LOCK_AFTER)
    settings_changes = [e for e in entries if e["tool_name"] == "settings_update"]
    failed_own = [e for e in entries if e.get("error") and is_autonomous(e.get("trigger")) and e not in refused]

    out = {
        "days": days,
        "done_on_own": len(done),
        "by_tool": dict(by_tool.most_common()),
        "held_for_ok": len(held),
        "refused": [{"tool": e["tool_name"], "error": e["error"][:160], "at": e["created_at"]} for e in refused[:20]],
        "wrong_pins": len(wrong_pins),
        "lockouts": lockouts,
        "settings_changes": len(settings_changes),
        "failed_on_own": [{"tool": e["tool_name"], "error": e["error"][:160], "at": e["created_at"]} for e in failed_own[:20]],
    }
    out["text"] = sentence(out)
    return out


def _count(entry: dict[str, Any]) -> int:
    error = entry.get("error") or ""
    try:
        return int(error.split("(", 1)[1].split(" ", 1)[0])
    except (IndexError, ValueError):
        return 0


def sentence(d: dict[str, Any]) -> str:
    period = "SirisAI's week" if d["days"] == 7 else f"SirisAI's last {d['days']:g} days"
    if d["done_on_own"]:
        top = ", ".join(_plural(n, *NOUNS.get(t, (t.replace("_", " "), t.replace("_", " ")))) for t, n in list(d["by_tool"].items())[:3])
        text = f"{period}: {_plural(d['done_on_own'], 'thing')} done on its own ({top})"
    else:
        text = f"{period}: nothing done on its own"
    text += f", {d['held_for_ok']} held for your OK, {len(d['refused'])} refused."
    if d["wrong_pins"]:
        text += f" {_plural(d['wrong_pins'], 'wrong PIN')}" + (f", {_plural(d['lockouts'], 'lockout')}." if d["lockouts"] else ".")
    else:
        text += " No wrong PINs."
    if d["settings_changes"]:
        text += f" Settings changed {_plural(d['settings_changes'], 'time')}."
    return text


def weekly(siris: "SirisAI", now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    found = digest(siris, 7, now)
    lines = [found["text"]]
    if found["by_tool"]:
        lines.append("Done on its own: " + ", ".join(f"{t.replace('_', ' ')} x{n}" for t, n in found["by_tool"].items()))
    for r in found["refused"][:8]:
        lines.append(f"Refused {r['tool'].replace('_', ' ')}: {r['error']}")
    for f in found["failed_on_own"][:8]:
        lines.append(f"Failed {f['tool'].replace('_', ' ')}: {f['error']}")
    siris.attention.raise_item(
        f"audit:weekly:{now:%G-W%V}",
        "alert",
        found["text"][:300],
        source="audit-weekly",
        severity="attention" if found["wrong_pins"] or found["refused"] else "info",
        body="\n".join(lines) + "\nThe full log is in the web UI's Activity tab.",
    )
    return found
