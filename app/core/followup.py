"""Following up an action that didn't take (6.38).

Every tool with a `verify()` (Home Assistant's service calls: lights,
switches, locks, covers, climate...) is checked right after it runs; that
check used to go only into the audit log. Now an action whose check fails
is checked again after FIRST_SECONDS (30) and AGAIN_SECONDS (120) — a
device asleep on Zigbee or a slow cover often catches up — and if it still
hasn't happened, the attention feed says so: "The kitchen light didn't turn
off (it's 'on')". Protocols and scenes run their steps through the same
tools, so their steps are followed up too. Kept in memory: a restart drops
pending checks rather than reporting stale ones.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.core.siris import SirisAI

logger = logging.getLogger(__name__)
FIRST_SECONDS = 30.0
AGAIN_SECONDS = 120.0
MAX_PENDING = 50
_lock = threading.Lock()
_pending: list[dict[str, Any]] = []


def later(name: str, arguments: dict[str, Any], result: Any, detail: str, trigger: str | None, now: float | None = None) -> None:
    now = time.monotonic() if now is None else now
    with _lock:
        _pending[:] = [p for p in _pending if not (p["name"] == name and p["arguments"] == arguments)][-(MAX_PENDING - 1):]
        _pending.append({"name": name, "arguments": arguments, "result": result, "detail": detail, "trigger": trigger,
                         "due": now + FIRST_SECONDS, "tries": 0, "since": now})


def pending() -> list[dict[str, Any]]:
    with _lock:
        return [dict(p) for p in _pending]


def clear() -> None:
    with _lock:
        _pending.clear()


def _what(name: str, arguments: dict[str, Any]) -> str:
    from app.voice.confirmations import describe_call

    try:
        return describe_call(name, arguments)
    except Exception:  # noqa: BLE001
        return name.replace("_", " ")


def check(siris: "SirisAI", now: float | None = None) -> list[dict[str, Any]]:
    """Re-check the actions that are due; returns the ones given up on (reported)."""
    now = time.monotonic() if now is None else now
    with _lock:
        due = [p for p in _pending if p["due"] <= now]
        _pending[:] = [p for p in _pending if p["due"] > now]
    reported = []
    for item in due:
        tool = siris.tools.get(item["name"])
        if tool is None:
            continue
        try:
            verification = tool.verify(item["arguments"], item["result"])
        except Exception as exc:  # noqa: BLE001 - can't check: say nothing rather than guess
            logger.info("Follow-up of %s couldn't check: %s", item["name"], exc)
            continue
        if verification.verified:
            continue
        item["tries"] += 1
        if item["tries"] < 2:
            item["due"] = now + (AGAIN_SECONDS - FIRST_SECONDS)
            with _lock:
                _pending.append(item)
            continue
        what = _what(item["name"], item["arguments"])
        siris.attention.raise_item(
            f"followup:{item['name']}:{hash(str(item['arguments'])) & 0xFFFFFFFF:x}", "alert",
            f"That didn't happen: {what}"[:200], source="followup", severity="attention",
            body=f"{verification.detail} Checked again {int(AGAIN_SECONDS / 60)} minutes later. The device may be offline or "
                 f"out of reach. (Asked by {item['trigger'] or 'you'}.)",
        )
        reported.append({**item, "detail": verification.detail})
    return reported
