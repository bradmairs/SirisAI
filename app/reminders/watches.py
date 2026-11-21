""""Tell me when..." (7.6): watch anything SirisAI can look up.

State reminders (app/reminders/conditions.py) watch one Home Assistant
device. A watch runs any read-only tool on a schedule and tells you once a
condition on its result holds:

- "tell me when Dune is on Plex": plex_search {"query": "Dune"}, text_contains "dune";
- "tell me when the Slow Horses download finishes": sonarr_queue, text_missing "slow horses";
- "tell me when the NAS has under 100 GB free": a disk tool, number_at "free_gb", below 100;
- "tell me when it stops raining": weather_current, text_missing "rain".

`number_at` is a dotted path into the result ("0.free_gb" for the first
row's free_gb); without one, the first number in the result is used. The
tool is run once when the watch is made: if the condition already holds
you're told straight away instead. Only READ tools can be watched, so a
watch can never change anything. It checks every `every_minutes` (at
least 5), fires once (a push and the feed), and gives up after `days`.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.core.siris import SirisAI

logger = logging.getLogger(__name__)
STATE_KEY = "reminders:watches"
MAX = 30
MIN_MINUTES = 5
_NUM = re.compile(r"-?\d+(?:\.\d+)?")


def _at(value: Any, path: str) -> Any:
    for part in [p for p in path.split(".") if p]:
        if isinstance(value, dict):
            value = value.get(part)
        elif isinstance(value, list) and part.lstrip("-").isdigit() and -len(value) <= int(part) < len(value):
            value = value[int(part)]
        else:
            return None
    return value


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    found = _NUM.search(json.dumps(value, default=str) if not isinstance(value, str) else value)
    return float(found.group()) if found else None


def holds(watch: dict[str, Any], result: Any) -> tuple[bool, str]:
    """Whether the condition holds on `result`, and what was seen."""
    text = json.dumps(result, default=str).lower()
    if watch.get("text_contains"):
        needle = watch["text_contains"].lower()
        return needle in text, f"found '{watch['text_contains']}'" if needle in text else ""
    if watch.get("text_missing"):
        needle = watch["text_missing"].lower()
        return needle not in text, f"'{watch['text_missing']}' is gone" if needle not in text else ""
    value = _number(_at(result, watch["number_at"]) if watch.get("number_at") else result)
    if value is None:
        return False, ""
    if watch.get("below") is not None and value < float(watch["below"]):
        return True, f"it's {value:g}"
    if watch.get("above") is not None and value > float(watch["above"]):
        return True, f"it's {value:g}"
    return False, f"{value:g}"


def create(
    siris: "SirisAI", what: str, tool: str, arguments: dict[str, Any] | None = None, *, text_contains: str = "",
    text_missing: str = "", number_at: str = "", below: float | None = None, above: float | None = None,
    every_minutes: int = 15, days: int = 7, now: datetime | None = None,
) -> dict[str, Any]:
    from app.core.user_context import get_current_user_id
    from app.plugins.base import ToolPermission

    found = siris.tools.get(tool)
    if found is None:
        raise ValueError(f"There's no tool called '{tool}'.")
    if found.permission is not ToolPermission.READ:
        raise ValueError(f"{tool} changes things; a watch can only use tools that look things up.")
    if not (text_contains or text_missing or below is not None or above is not None):
        raise ValueError("Say what to wait for: text_contains, text_missing, or below/above a number.")
    items = list(siris.state.get(STATE_KEY) or [])
    if len(items) >= MAX:
        raise ValueError(f"There are already {MAX} watches; cancel one first.")
    now = now or datetime.now(UTC)
    watch = {
        "id": max((w["id"] for w in items), default=0) + 1, "what": what.strip() or tool, "tool": tool,
        "arguments": arguments or {}, "text_contains": text_contains, "text_missing": text_missing,
        "number_at": number_at, "below": below, "above": above,
        "every_minutes": max(MIN_MINUTES, int(every_minutes or 15)), "until": (now + timedelta(days=max(1, int(days or 7)))).isoformat(),
        "next": now.isoformat(), "user": get_current_user_id(), "created": now.isoformat(),
    }
    result = siris.run_tool(tool, watch["arguments"], trigger=f"watch: checking now for '{watch['what']}'")
    hit, seen = holds(watch, result)
    if hit:
        return {"already": True, "what": watch["what"], "seen": seen, "watching": False}
    watch["next"] = (now + timedelta(minutes=watch["every_minutes"])).isoformat()
    siris.state.set(STATE_KEY, [*items, watch])
    return {"watching": True, **{k: watch[k] for k in ("id", "what", "every_minutes", "until")}, "now": seen or None}


def cancel(siris: "SirisAI", watch_id: int) -> bool:
    items = list(siris.state.get(STATE_KEY) or [])
    kept = [w for w in items if w["id"] != watch_id]
    siris.state.set(STATE_KEY, kept)
    return len(kept) < len(items)


def check(siris: "SirisAI", now: datetime | None = None) -> list[dict[str, Any]]:
    """Run the watches that are due; fire and drop those whose condition holds."""
    now = now or datetime.now(UTC)
    items = list(siris.state.get(STATE_KEY) or [])
    if not items:
        return []
    fired, kept = [], []
    for w in items:
        if datetime.fromisoformat(w["until"]) < now:
            siris.attention.raise_item(f"watch:{w['id']}", "alert", f"Stopped watching: {w['what']}"[:200], source="watches",
                                       severity="info", body="It didn't happen within the time set.")
            continue
        if datetime.fromisoformat(w["next"]) > now:
            kept.append(w)
            continue
        w["next"] = (now + timedelta(minutes=w["every_minutes"])).isoformat()
        try:
            result = siris.run_tool(w["tool"], w["arguments"], trigger=f"watch: '{w['what']}'")
        except Exception:  # noqa: BLE001 - the service is down: try again next time
            kept.append(w)
            continue
        hit, seen = holds(w, result)
        if not hit:
            kept.append(w)
            continue
        text = f"{w['what'][0].upper()}{w['what'][1:]}" + (f" ({seen})" if seen else "")
        siris.attention.raise_item(f"watch:{w['id']}", "alert", text[:200], source="watches", severity="attention")
        if siris.tools.get("notify") is not None:
            try:
                siris.run_tool("notify", {"title": "SirisAI", "message": text}, trigger=f"watch: '{w['what']}' happened")
            except Exception:  # noqa: BLE001 - it's in the feed either way
                logger.info("Couldn't push a watch", exc_info=True)
        fired.append(w)
    siris.state.set(STATE_KEY, kept)
    return fired
