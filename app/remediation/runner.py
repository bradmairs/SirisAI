"""Self-healing: when a watched container goes down, try the known fix
before bothering anyone -- and stop trying when it isn't working.

The runbook for a watched container (SIRISAI_REMEDIATION_CONTAINERS) that is
exited, dead, stuck restarting, or reporting "(unhealthy)":

1. Restart it through docker_restart_container -- auto-confirmed, because
   listing a container here is the owner's standing approval for exactly this
   action on exactly that container -- and let that tool's own verify() say
   whether it actually came back.
2. Fixed: queue a note for the next briefing ("plex was down; restarted, it's
   running again"). No push -- nothing needs doing.
3. Not fixed, or not allowed to try (this container already had
   SIRISAI_REMEDIATION_MAX_RESTARTS_PER_CONTAINER restarts in the past hour,
   or SIRISAI_REMEDIATION_MAX_ACTIONS_PER_HOUR is used up across all
   containers): read the last log lines and push an urgent alert with them,
   once per container per hour.

The budget is the point: a container in a crash loop gets a couple of
restarts, then a human with the logs -- never an endless restart loop that
hides the real problem. Every action is in the audit log with a
"self-healing" trigger; the ledger of attempts lives in app_state.
"""

import time
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.core.siris import SirisAI

LEDGER_KEY = "remediation_ledger"
_WINDOW = timedelta(hours=1)
_LOG_LINES = 15


def unhealthy_reason(container: dict[str, Any]) -> str | None:
    state = (container.get("state") or "").lower()
    status = (container.get("status") or "").lower()
    if state in ("exited", "dead"):
        return f"it has {state} ({container.get('status')})"
    if state == "restarting":
        return "it is stuck restarting"
    if "(unhealthy)" in status:
        return "its health check is failing"
    return None


def _ledger(siris: "SirisAI", now: datetime) -> dict[str, list[dict[str, Any]]]:
    ledger = siris.state.get(LEDGER_KEY) or {"actions": [], "alerts": []}
    cutoff = now - _WINDOW
    for kind in ("actions", "alerts"):
        ledger[kind] = [e for e in ledger.get(kind, []) if datetime.fromisoformat(e["at"]) > cutoff]
    return ledger


def run(siris: "SirisAI", now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    watched = siris.settings.remediation_container_list()
    client = siris._docker_client
    if client is None or not watched:
        return {"watched": watched, "handled": []}

    ledger = _ledger(siris, now)
    containers = client.list_containers(all_containers=True)
    handled = []
    for container in containers:
        name = next((n for n in container["names"] if n in watched or "*" in watched), None)
        if name is None:
            continue
        reason = unhealthy_reason(container)
        if reason is None:
            continue
        handled.append(_heal(siris, name, reason, ledger, now))
    siris.state.set(LEDGER_KEY, ledger)
    return {"watched": watched, "handled": handled, "actions_this_hour": len(ledger["actions"])}


def _heal(siris: "SirisAI", name: str, reason: str, ledger: dict[str, Any], now: datetime) -> dict[str, Any]:
    trigger = f"self-healing: watched container '{name}' is down ({reason})"
    settings = siris.settings
    mine = [a for a in ledger["actions"] if a["container"] == name]
    blocked = None
    if len(mine) >= settings.remediation_max_restarts_per_container:
        blocked = f"it was already restarted {len(mine)} time(s) in the last hour"
    elif len(ledger["actions"]) >= settings.remediation_max_actions_per_hour:
        blocked = f"the self-healing budget ({settings.remediation_max_actions_per_hour} actions an hour) is used up"

    outcome: dict[str, Any] = {"container": name, "reason": reason}
    if blocked is None:
        verified = False
        detail = ""
        try:
            siris.run_tool("docker_restart_container", {"container": name}, confirmed=True, trigger=trigger)
            verified, detail = _still_up_after_settling(siris, name)
        except Exception as exc:
            detail = str(exc)[:200]
        ledger["actions"].append({"container": name, "at": now.isoformat(), "fixed": verified})
        outcome.update({"action": "restarted", "fixed": verified, "detail": detail})
        if verified:
            siris.pending_notifications.queue(
                f"{name} was down ({reason}); I restarted it and it's running again.", severity="info", category="self_healing"
            )
            return outcome
        blocked = f"a restart didn't bring it back ({detail or 'no detail'})"
    else:
        outcome.update({"action": "none", "fixed": False, "detail": blocked})

    if any(a["container"] == name for a in ledger["alerts"]):
        outcome["escalated"] = "already alerted this hour"
        return outcome
    logs = ""
    try:
        logs = siris.run_tool("docker_get_container_logs", {"container": name, "tail": _LOG_LINES}, trigger=trigger)
    except Exception:
        logs = "(couldn't read its logs)"
    lines = logs.get("lines", []) if isinstance(logs, dict) else str(logs).splitlines()
    tail = "\n".join(lines[-_LOG_LINES:])
    message = f"{name} is down ({reason}) and I couldn't fix it: {blocked}.\nLast log lines:\n{tail}"
    if siris.tools.get("notify") is not None:
        siris.run_tool("notify", {"title": f"SirisAI: {name} needs you", "message": message[:3500], "priority": "urgent"}, trigger=trigger)
    else:
        siris.pending_notifications.queue(message[:1000], severity="urgent", category="self_healing")
    ledger["alerts"].append({"container": name, "at": now.isoformat()})
    outcome["escalated"] = "alerted with logs"
    return outcome


def _still_up_after_settling(siris: "SirisAI", name: str) -> tuple[bool, str]:
    """Whether the container is still healthy SIRISAI_REMEDIATION_SETTLE_SECONDS
    after the restart. docker_restart_container's own verify() looks right
    away, and a container that crashes on start is briefly "running" then --
    found live: an always-crashing container was reported fixed."""
    time.sleep(siris.settings.remediation_settle_seconds)
    assert siris._docker_client is not None
    match = next((c for c in siris._docker_client.list_containers(all_containers=True) if name in c["names"]), None)
    if match is None:
        return False, "it disappeared after the restart"
    reason = unhealthy_reason(match)
    if reason is not None:
        return False, f"{reason} again"
    return True, f"running ({match.get('status')})"


def status(siris: "SirisAI", now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    ledger = _ledger(siris, now)
    return {
        "watched": siris.settings.remediation_container_list(),
        "actions_this_hour": ledger["actions"],
        "alerts_this_hour": ledger["alerts"],
        "budget_left_this_hour": max(0, siris.settings.remediation_max_actions_per_hour - len(ledger["actions"])),
    }
