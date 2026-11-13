"""Following up a reboot or shutdown (5.73).

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
    _timeline(siris, server, f"{action} started" + (f" (in {delay_minutes} min)" if delay_minutes else ""), now)


def _timeline(siris: "SirisAI", server: str, text: str, at: datetime | None = None) -> None:
    from app.servers import timeline

    try:
        timeline.add(siris, server, "power", text, at)
    except Exception:  # noqa: BLE001
        logger.debug("Couldn't add to the server timeline", exc_info=True)


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
    words = {"back": "rebooted and came back", "not rebooted": "didn't reboot", "not back": "didn't come back after rebooting",
             "off": "shut down", "still up": "didn't shut down"}
    for item in finished:
        _timeline(siris, item["server"], words.get(item["outcome"], item["outcome"]), now)
    return finished


# -- 6.38: a reboot at a set time, and the servers that need one, one at a time --------------

SCHEDULED_KEY = "power:scheduled"
ROLLING_KEY = "power:rolling"
LATE_MINUTES = 30  # a scheduled reboot SirisAI was down for isn't done this late


def parse_when(siris: "SirisAI", text: str, now: datetime | None = None) -> datetime:
    """'03:00', 'sunday 03:00', 'tomorrow 3am', '2026-11-15T03:00': the next such time, local, as UTC."""
    import re

    from app.core import clock

    now = now or datetime.now(UTC)
    zone = clock.get_zone(siris.settings.timezone)
    local_now = now.astimezone(zone)
    t = (text or "").strip().lower()
    try:
        when = datetime.fromisoformat(t.upper() if "t" in t and t[:4].isdigit() else t)
        when = when if when.tzinfo else when.replace(tzinfo=zone)
        if when <= now:
            raise ValueError("That time has passed.")
        return when.astimezone(UTC)
    except ValueError as exc:
        if "passed" in str(exc):
            raise
    m = re.fullmatch(r"(?:(?P<day>today|tonight|tomorrow|mon|tue|wed|thu|fri|sat|sun)[a-z]*\s+)?(?:at\s+)?"
                     r"(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ap>am|pm)?", t)
    if not m:
        raise ValueError(f"Say when as a time like 03:00 or 'sunday 3am', not {text!r}.")
    hour, minute = int(m.group("h")), int(m.group("m") or 0)
    if m.group("ap") == "pm" and hour < 12:
        hour += 12
    if m.group("ap") == "am" and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        raise ValueError(f"{text!r} isn't a time of day.")
    day = m.group("day")
    days = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    candidate = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if day == "tomorrow":
        candidate += timedelta(days=1)
    elif day in days:
        candidate += timedelta(days=(days.index(day) - local_now.weekday()) % 7)
        if candidate <= local_now:
            candidate += timedelta(days=7)
    elif candidate <= local_now:
        candidate += timedelta(days=1)
    return candidate.astimezone(UTC)


def schedule(siris: "SirisAI", action: str, servers: list[str], at: datetime) -> dict[str, Any]:
    items = list(siris.state.get(SCHEDULED_KEY) or [])
    entry = {"id": (max((i["id"] for i in items), default=0) + 1), "action": action, "servers": servers, "at": at.isoformat()}
    siris.state.set(SCHEDULED_KEY, [*items, entry])
    return entry


def scheduled(siris: "SirisAI") -> list[dict[str, Any]]:
    return list(siris.state.get(SCHEDULED_KEY) or [])


def cancel_scheduled(siris: "SirisAI", servers: list[str]) -> list[dict[str, Any]]:
    """Drop these servers from scheduled reboots and any rolling reboot; returns what was dropped."""
    dropped, kept = [], []
    for item in scheduled(siris):
        left = [s for s in item["servers"] if s not in servers]
        if len(left) < len(item["servers"]):
            dropped.append({**item, "servers": [s for s in item["servers"] if s in servers]})
        if left:
            kept.append({**item, "servers": left})
    siris.state.set(SCHEDULED_KEY, kept)
    rolling = siris.state.get(ROLLING_KEY)
    if rolling:
        queue = [s for s in rolling.get("queue", []) if s not in servers]
        if len(queue) < len(rolling.get("queue", [])):
            dropped.append({"rolling": True, "servers": [s for s in rolling["queue"] if s in servers]})
        siris.state.set(ROLLING_KEY, {**rolling, "queue": queue} if queue or rolling.get("current") else None)
    return dropped


def _power(siris: "SirisAI", action: str, servers: list[str], why: str) -> dict[str, Any]:
    """Run ssh_power for a reboot the user already approved (when scheduling it)."""
    tool = siris.tools.get("ssh_power")
    if tool is None:
        raise RuntimeError("Reboots aren't set up (SIRISAI_SSH_HOSTS).")
    args = {"action": action, "servers": ",".join(servers)}
    try:
        result = tool.run(**args)
    except Exception as exc:
        siris.audit.record("ssh_power", args, error=str(exc), trigger=why)
        raise
    siris.audit.record("ssh_power", args, result=result, trigger=why)
    return result


def start_rolling(siris: "SirisAI", servers: list[str], at: datetime | None = None) -> dict[str, Any]:
    """Reboot these one at a time: each must be back before the next goes down."""
    own = (siris.settings.ssh_self_server or "").strip().lower()
    queue = sorted(dict.fromkeys(servers), key=lambda s: s.lower() == own)  # SirisAI's own server last
    state = {"queue": queue, "current": None, "done": [], "at": (at or datetime.now(UTC)).isoformat()}
    siris.state.set(ROLLING_KEY, state)
    return state


def _advance_rolling(siris: "SirisAI", finished: list[dict[str, Any]], now: datetime) -> None:
    state = siris.state.get(ROLLING_KEY)
    if not state or now < datetime.fromisoformat(state["at"]):
        return
    current = state.get("current")
    if current:
        outcome = next((f["outcome"] for f in finished if f["server"] == current), None)
        if outcome is None:
            if any(p["server"] == current for p in pending(siris)):
                return  # still on its way back
            outcome = "back"  # followed up already
        if outcome != "back":
            siris.attention.raise_item(
                "power:rolling", "alert", f"Stopped rebooting the servers: {current} {outcome}", source="servers-power",
                body=f"Done: {', '.join(state['done']) or 'none'}. Not started: {', '.join(state['queue']) or 'none'}. "
                     "Ask again once it's sorted.")
            siris.state.set(ROLLING_KEY, None)
            return
        state = {**state, "current": None, "done": [*state["done"], current]}
    if not state["queue"]:
        siris.attention.raise_item("power:rolling", "notice", f"Rebooted {', '.join(state['done'])}, one at a time; all back",
                                   source="servers-power", severity="info")
        siris.state.set(ROLLING_KEY, None)
        return
    nxt, rest = state["queue"][0], state["queue"][1:]
    try:
        result = _power(siris, "reboot", [nxt], f"rolling reboot you asked for ({len(state['done']) + 1} of {len(state['done']) + 1 + len(rest)})")
        ok = any(r.get("status") != "failed" for r in result.get("results", []))
    except Exception as exc:  # noqa: BLE001
        ok, result = False, {"summary": str(exc)}
    if not ok:
        siris.attention.raise_item("power:rolling", "alert", f"Stopped rebooting the servers: {nxt} didn't start",
                                   source="servers-power", body=str(result.get("summary"))[:400])
        siris.state.set(ROLLING_KEY, None)
        return
    siris.state.set(ROLLING_KEY, {**state, "current": nxt, "queue": rest})


def _run_scheduled(siris: "SirisAI", now: datetime) -> None:
    due, later = [], []
    for item in scheduled(siris):
        (due if datetime.fromisoformat(item["at"]) <= now else later).append(item)
    if not due:
        return
    siris.state.set(SCHEDULED_KEY, later)
    for item in due:
        at = datetime.fromisoformat(item["at"])
        names = ", ".join(item["servers"])
        if now - at > timedelta(minutes=LATE_MINUTES):
            siris.attention.raise_item(f"power:scheduled:{item['id']}", "alert", f"The {item['action']} of {names} didn't happen",
                                       source="servers-power", body=f"It was set for {at.isoformat()[:16]} UTC but SirisAI wasn't running "
                                       "then, and it isn't done this late on its own. Ask again if you still want it.")
            continue
        if item["action"] == "rolling":
            start_rolling(siris, item["servers"], now)
            continue
        try:
            result = _power(siris, item["action"], item["servers"], f"scheduled by you for {at.isoformat()[:16]} UTC")
            title = result.get("summary") or f"{item['action'].capitalize()} of {names} started"
            siris.attention.raise_item(f"power:scheduled:{item['id']}", "notice", title[:200], source="servers-power", severity="info")
        except Exception as exc:  # noqa: BLE001
            siris.attention.raise_item(f"power:scheduled:{item['id']}", "alert", f"The scheduled {item['action']} of {names} failed",
                                       source="servers-power", body=str(exc)[:400])


def tick(siris: "SirisAI", now: datetime | None = None) -> list[dict[str, Any]]:
    """The scheduler's power job: scheduled reboots, follow-ups, then the rolling reboot."""
    now = now or datetime.now(UTC)
    finished = check(siris, now)
    _advance_rolling(siris, finished, now)
    _run_scheduled(siris, now)  # last, so what it starts is first checked on the next tick
    return finished
