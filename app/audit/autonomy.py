"""What SirisAI did on its own: the audit log filtered to actions nobody
asked for in the moment -- the scheduler's checks, self-healing, intruder
alerts, energy jobs, presence scenes, routines, habit suggestions, memory it
decided to keep -- as a readable timeline ("what have you done today?").

Classified by the audit entry's `trigger`, which every autonomous code path
already sets (milestone 28). Read-only lookups are left out by default --
the timeline is about things SirisAI *did* (changed, sent, restarted,
remembered), not everything it looked at.
"""

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from app.core import clock
from app.plugins.base import ToolPermission

if TYPE_CHECKING:
    from app.core.siris import SirisAI

_AUTONOMOUS_PREFIXES = (
    "proactive check",
    "presence_check",
    "intruder_check",
    "self-healing",
    "energy scheduler",
    "security watch",
    "habit check",
    "routine",
    "automatic memory extraction",
    "geofence",
    "event_reaction",
    "presence:",
    "away mode",
    "camera digest",
    "internet log",
)

# Scheduled watchers that run every few minutes and usually find nothing.
# What they *do* when something happens (a reminder sent, a scene turned on,
# a departure heads-up) is audited separately under its own trigger, so the
# check itself is left out -- otherwise "Geofence reminder check" filled the
# timeline every three minutes.
_WATCHERS = frozenset({"geofence_reminder_check", "presence_check"})
_LABELS = {
    "notify": "sent a notification",
    "docker_restart_container": "restarted a container",
    "home_assistant_call_service": "changed a device",
    "speak_in_current_room": "spoke in the current room",
    "remember": "remembered something",
    "remember_entity_fact": "learned a fact",
    "run_protocol": "ran a protocol",
}


def is_autonomous(trigger: str | None) -> bool:
    return bool(trigger) and trigger.lower().startswith(_AUTONOMOUS_PREFIXES)


def _describe(entry: dict[str, Any]) -> str:
    args = entry.get("arguments") or {}
    tool = entry["tool_name"]
    if tool == "notify":
        title = args.get("title") or "a notification"
        return f"Sent '{title}': {str(args.get('message', ''))[:140]}"
    if tool == "home_assistant_call_service":
        return f"{str(args.get('service', '')).replace('_', ' ').capitalize()} {args.get('entity_id') or args.get('domain')}"
    if tool == "docker_restart_container":
        return f"Restarted {args.get('container')}"
    if tool == "speak_in_current_room":
        return f"Said: {str(args.get('message', ''))[:140]}"
    if tool in ("remember", "remember_entity_fact"):
        return f"Remembered: {str(args.get('content') or args.get('fact') or '')[:140]}"
    detail = ", ".join(f"{k}={v}" for k, v in list(args.items())[:2])
    return f"{_LABELS.get(tool, tool.replace('_', ' '))}" + (f" ({detail})" if detail else "")


def timeline(siris: "SirisAI", hours: float = 24, include_reads: bool = False, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    entries = siris.audit.since(now - timedelta(hours=hours))
    items = []
    for entry in entries:
        if not is_autonomous(entry.get("trigger")) or entry["tool_name"] in _WATCHERS:
            continue
        tool = siris.tools.get(entry["tool_name"])
        if not include_reads and tool is not None and tool.permission is ToolPermission.READ:
            continue
        created = datetime.fromisoformat(entry["created_at"]).replace(tzinfo=UTC)
        items.append(
            {
                "id": entry.get("id"),
                "area": _area(tool, entry["tool_name"]),
                "details": f"/siris/activity?id={entry.get('id')}",
                "time": created.isoformat(),
                "local_time": created.astimezone(clock.get_zone(siris.settings.timezone)).strftime("%H:%M"),
                "tool": entry["tool_name"],
                "what": _describe(entry),
                "why": entry.get("trigger"),
                "outcome": "failed" if entry.get("error") else "held for confirmation" if entry.get("confirmation_required") else "done",
                "verified": entry.get("verified"),
            }
        )
    items.reverse()
    _mark_undo(siris, items, now)
    by_area: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        by_area.setdefault(item["area"], []).append(item)
    if items:
        parts = []
        for area, group in sorted(by_area.items(), key=lambda kv: -len(kv[1])):
            failed = sum(1 for i in group if i["outcome"] == "failed")
            shown = "; ".join(f"{i['local_time']} {i['what']}" for i in group[:3]) + ("; ..." if len(group) > 3 else "")
            parts.append(f"{AREA_NAMES.get(area, area)} ({len(group)}{f', {failed} failed' if failed else ''}): {shown}")
        summary = (f"In the last {hours:g} hours I took {len(items)} action{'s' if len(items) != 1 else ''} on my own. "
                   + ". ".join(parts) + ".")
        if any(i.get("undo") for i in items):
            summary += " The last one can be undone: say 'undo that'."
    else:
        summary = f"I haven't done anything on my own in the last {hours:g} hours."
    groups = [{"area": a, "name": AREA_NAMES.get(a, a), "count": len(g), "items": g} for a, g in sorted(by_area.items(), key=lambda kv: -len(kv[1]))]
    return {"summary": summary, "items": items, "by_area": groups}


# 6.38: grouped by area, each with a details link and, for the last one, undo.
AREA_NAMES = {
    "home_assistant": "Home", "home": "Home", "scenes": "Home", "protocols": "Protocols", "modes": "Modes",
    "docker": "Containers", "servers": "Servers", "ssh": "Servers", "cron": "Cron", "notify": "Notifications",
    "memory": "Memory", "reminders": "Reminders", "energy": "Energy", "health": "Health", "misc": "Other",
}


def _area(tool: Any, name: str) -> str:
    if name.startswith(("ssh_", "server_")):
        return "servers"
    if name == "notify":
        return "notify"
    if tool is None:
        return "misc"
    from app.core.siris import _categorize_tool

    try:
        return _categorize_tool(tool)
    except Exception:  # noqa: BLE001
        return "misc"


def _mark_undo(siris: "SirisAI", items: list[dict[str, Any]], now: datetime) -> None:
    from app.core import undo

    try:
        last = undo.last(siris, now)
    except Exception:  # noqa: BLE001
        return
    if not last:
        return
    at = datetime.fromisoformat(last["at"])
    for item in items:
        if abs((datetime.fromisoformat(item["time"]) - at).total_seconds()) <= 10:
            item["undo"] = f"Say 'undo that': {last['label']}"
            break
