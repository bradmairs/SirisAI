"""Reminders on a device's state (6.58): "remind me to hang the washing out
when the washing machine finishes".

create_state_reminder watches one Home Assistant entity and fires once:

- `to_state`: when it changes to that state (a dishwasher's `off`, a door's
  `open`), not if it's already in it;
- `below`: when a number (a smart plug's power, in watts) drops to or under
  it after having been above it for at least one check, which is how "the
  cycle finished" looks on a plug: "below 5" W.

The `state_reminders` job checks every minute. A fired reminder goes out as
a push and into the feed. "When I get home" and "near the shops" are the
geofence reminders (create_geofence_reminder), which already exist.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from app.core.user_context import get_current_user_id

if TYPE_CHECKING:
    from app.core.siris import SirisAI

logger = logging.getLogger(__name__)
STATE_KEY = "reminders:state"
MAX = 30


def _client(siris: "SirisAI") -> Any:
    client = getattr(siris, "_home_assistant_client", None)
    if client is None:
        raise RuntimeError("Home Assistant isn't configured.")
    return client


def _number(text: Any) -> float | None:
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def create(siris: "SirisAI", entity_id: str, message: str, to_state: str | None = None, below: float | None = None) -> dict[str, Any]:
    if not message.strip():
        raise ValueError("A reminder needs a message.")
    if (to_state is None) == (below is None):
        raise ValueError("Give either to_state (e.g. 'off') or below (e.g. 5 for watts), not both.")
    state = _client(siris).get_state_or_none(entity_id)
    if state is None:
        raise ValueError(f"There's no Home Assistant entity '{entity_id}'. Look it up with home_assistant_resolve_entity.")
    items = list(siris.state.get(STATE_KEY) or [])
    if len(items) >= MAX:
        raise ValueError(f"There are already {MAX} of these waiting; cancel some first.")
    name = (state.get("attributes") or {}).get("friendly_name") or entity_id
    current = state.get("state")
    item = {"id": max((i["id"] for i in items), default=0) + 1, "user": get_current_user_id(), "entity_id": entity_id, "name": name,
            "message": message.strip(), "to_state": to_state, "below": below, "created": datetime.now(UTC).isoformat(),
            "last": current, "armed": below is None or (_number(current) or 0) > below}
    siris.state.set(STATE_KEY, [*items, item])
    when = f"changes to {to_state}" if to_state is not None else f"drops below {below:g}"
    return {**item, "message_text": f"I'll remind you to {message.strip()} when {name} {when}."}


def listed(siris: "SirisAI") -> list[dict[str, Any]]:
    return [i for i in siris.state.get(STATE_KEY) or [] if i.get("user") == get_current_user_id()]


def cancel(siris: "SirisAI", reminder_id: int) -> dict[str, Any]:
    items = list(siris.state.get(STATE_KEY) or [])
    kept = [i for i in items if not (i["id"] == reminder_id and i.get("user") == get_current_user_id())]
    if len(kept) == len(items):
        raise LookupError(f"No waiting reminder {reminder_id}.")
    siris.state.set(STATE_KEY, kept)
    return {"cancelled": reminder_id}


def _fire(siris: "SirisAI", item: dict[str, Any], state: str) -> None:
    text = f"Reminder: {item['message']} ({item['name']} is {state})"
    siris.attention.raise_item(f"reminder:state:{item['id']}", "alert", text[:200], source="reminders", severity="attention")
    if siris.tools.get("notify") is not None:
        try:
            siris.run_tool("notify", {"message": text, "title": "Reminder"}, trigger=f"state reminder: {item['name']}")
        except Exception:  # noqa: BLE001 - it's in the feed either way
            logger.info("Couldn't push a state reminder", exc_info=True)


def check(siris: "SirisAI") -> list[dict[str, Any]]:
    items = list(siris.state.get(STATE_KEY) or [])
    if not items:
        return []
    client = _client(siris)
    fired, kept = [], []
    for item in items:
        try:
            state = client.get_state_or_none(item["entity_id"])
        except Exception:  # noqa: BLE001 - try again next minute
            kept.append(item)
            continue
        current = (state or {}).get("state")
        hit = False
        if item["to_state"] is not None:
            hit = current == item["to_state"] and item.get("last") != item["to_state"]
        else:
            value = _number(current)
            if value is not None and value > item["below"]:
                item["armed"] = True
            hit = bool(item.get("armed")) and value is not None and value <= item["below"]
        item["last"] = current
        if hit:
            _fire(siris, item, str(current))
            fired.append(item)
        else:
            kept.append(item)
    siris.state.set(STATE_KEY, kept)
    return fired
