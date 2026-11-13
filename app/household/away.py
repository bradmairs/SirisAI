"""Away mode: when presence says everyone's out, offer (or run) the away
protocol; when someone's back, undo it.

- **Leaving** (SIRISAI_AWAY_MODE, "ask"): "Everyone's out. Run the away
  protocol?" goes to the attention feed and your phone, listing what it would
  do, with **Run it**. "auto" runs it straight away and says so; "off" leaves
  it alone. The protocol is SIRISAI_AWAY_PROTOCOL ("away" by default: lights
  and media off, doors locked, garage closed; make your own as a protocol
  to change it).
- **Arriving**: if away mode ran the protocol and nothing else ran since,
  its undo is applied, except for anything that would unlock a door, open
  the garage or a gate, or disarm an alarm. Those stay secure; a phone
  joining the Wi-Fi isn't proof enough to open the house. An unanswered
  offer closes itself.

Called from presence_check on a change (app/skills/builtin/presence.py).
"""

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from app.hub.attention import action, register_action
from app.protocols import service as protocols

if TYPE_CHECKING:
    from app.core.siris import SirisAI

logger = logging.getLogger(__name__)

STATE_KEY = "away_mode"
OFFER_KEY = "away:offer"
# Undo steps that would make the house less secure: never automatic.
UNSAFE_SERVICES = {"unlock", "open_cover", "open", "alarm_disarm", "open_valve"}


AWAY_LOG_KEY = "presence:away_log"


def log_change(siris: "SirisAI", home: bool, now: datetime | None = None) -> None:
    """6.48: when everyone was out, as [start, end] pairs (end None while out), the last 60."""
    at = (now or datetime.now(UTC)).isoformat()
    log = [list(p) for p in siris.state.get(AWAY_LOG_KEY) or []]
    if not home and not (log and log[-1][1] is None):
        log.append([at, None])
    elif home and log and log[-1][1] is None:
        log[-1][1] = at
    siris.state.set(AWAY_LOG_KEY, log[-60:])


def away_periods(siris: "SirisAI", now: datetime | None = None) -> list[tuple[datetime, datetime]]:
    now = now or datetime.now(UTC)
    return [(datetime.fromisoformat(a), datetime.fromisoformat(b) if b else now) for a, b in siris.state.get(AWAY_LOG_KEY) or []]


def on_change(siris: "SirisAI", home: bool, now: datetime | None = None) -> dict[str, Any] | None:
    try:
        log_change(siris, home, now)
    except Exception:  # noqa: BLE001
        logger.debug("Couldn't log the away period", exc_info=True)
    mode = getattr(siris.settings, "away_mode", "off")
    if mode == "off":
        return None
    try:
        from app.travel import service as travel

        if mode == "ask" and travel.away_active(siris, now):
            mode = "auto"  # "Away while I'm gone" on a trip (app/travel/service.py)
    except Exception:  # noqa: BLE001
        logger.debug("Checking for a trip failed", exc_info=True)
    try:
        return arrived(siris, now) if home else left(siris, mode, now)
    except Exception:  # noqa: BLE001 - presence itself must carry on
        logger.exception("Away mode failed")
        return None


def left(siris: "SirisAI", mode: str, now: datetime | None = None) -> dict[str, Any] | None:
    name = siris.settings.away_protocol
    preview = protocols.preview(siris, name)
    if not preview["found"]:
        return {"skipped": f"there's no protocol called '{name}'"}
    if not preview.get("active", True):
        return {"skipped": f"'{name}' is turned off"}  # 5.11: not run, not offered
    loud = preview.get("notify", True)  # 5.11: notifications off: it stays in the feed, nothing goes to the phone
    steps = [s["step"] for s in preview["steps"] if s["will_run"]]
    if mode == "auto":
        result = run(siris, "presence: everyone left (away mode is automatic)", now)
        siris.attention.raise_item(
            "away:ran", "alert", f"Everyone's out, so I ran {name}", source="away", severity="info",
            body="Done: " + ", ".join(steps) + ". It's undone when someone's back (doors stay locked).",
        )
        if loud:
            _push(siris, f"Everyone's out, so I ran {name}: " + ", ".join(steps) + ".")
        return result
    siris.attention.raise_item(
        OFFER_KEY,
        "suggestion",
        f"Everyone's out. Run {name}?",
        source="away",
        severity="attention" if loud else "info",
        body="It would: " + ", ".join(steps) + ". When someone's back it's undone, except that doors stay locked and the garage stays shut.",
        actions=[action("run", "Run it", "primary"), action("dismiss", "Not now")],
    )
    if loud:
        _push(siris, f"Everyone's out. Run {name}? It would: " + ", ".join(steps) + ". Open SirisAI to run it.")
    return {"offered": name, "steps": steps}


def run(siris: "SirisAI", trigger: str, now: datetime | None = None) -> dict[str, Any]:
    result = protocols.run(siris, siris.settings.away_protocol, trigger)
    last = siris.state.get(protocols.LAST_RUN_KEY) or {}
    siris.state.set(STATE_KEY, {"ran_at": last.get("ran_at") or (now or datetime.now(UTC)).isoformat(), "protocol": result["protocol"]})
    return result


def arrived(siris: "SirisAI", now: datetime | None = None) -> dict[str, Any] | None:
    for item in siris.attention.list("open", limit=500):
        if item["key"] in (OFFER_KEY, "away:ran"):
            siris.attention.close(item["id"], "done" if item["key"] == "away:ran" else "dismissed", "Someone's home")
    ours = siris.state.get(STATE_KEY)
    last = siris.state.get(protocols.LAST_RUN_KEY) or {}
    siris.state.delete(STATE_KEY)
    if not ours or last.get("ran_at") != ours.get("ran_at") or last.get("undone"):
        return None  # away mode didn't run it, or something else ran since
    safe = [s for s in last.get("undo") or [] if (s.get("arguments") or {}).get("service") not in UNSAFE_SERVICES]
    kept = len(last.get("undo") or []) - len(safe)
    results = []
    for step in reversed(safe):
        try:
            siris.run_tool(step["tool"], step["arguments"], confirmed=True, trigger=f"presence: someone's home, undoing '{last['protocol']}'")
            results.append({"tool": step["tool"], "arguments": step["arguments"], "status": "ok"})
        except Exception as exc:  # noqa: BLE001 - restore what can be restored
            results.append({"tool": step["tool"], "arguments": step["arguments"], "status": "error", "error": str(exc)[:200]})
    last["undone"] = True
    siris.state.set(protocols.LAST_RUN_KEY, last)
    return {"undone": last["protocol"], "steps": results, "kept_secure": kept}


def _push(siris: "SirisAI", message: str) -> None:
    if siris.tools.get("notify") is None:
        return
    try:
        siris.run_tool("notify", {"title": "SirisAI: away", "message": message}, trigger="away mode")
    except Exception:  # noqa: BLE001
        logger.exception("Away mode push failed")


def _run_action(siris: "SirisAI", item: dict[str, Any], payload: dict[str, Any], trigger: str) -> tuple[Any, str]:
    result = run(siris, trigger)
    done = sum(1 for s in result["steps"] if s["status"] == "ok")
    return result, f"Ran {result['protocol']} ({done} step{'s' if done != 1 else ''})"


register_action("away", "run", _run_action)
