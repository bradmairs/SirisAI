"""House problems caught early (SirisAI 5.0), from Home Assistant's
sensors, checked every two minutes:

- **Water**: any moisture sensor (device_class moisture, or "leak"/"water"
  in its name) that's wet: urgent, pushed straight away.
- **Fridge and freezer**: a temperature sensor named fridge or freezer
  above SIRISAI_FRIDGE_MAX_C (6) or SIRISAI_FREEZER_MAX_C (-12) for
  SIRISAI_APPLIANCE_WARM_MINUTES (20): the door's open or it's failing.
- **Left open**: a door, garage or gate (binary sensor or cover) open for
  SIRISAI_DOOR_OPEN_MINUTES (15), and nobody's said it's fine. Windows
  aren't counted (they're often left open on purpose).
- **Batteries**: any battery sensor under 20%, in one weekly line.

Each closes itself when the problem's gone. And on the first of each month,
a **house health** checklist: filters, smoke alarm test, gutters, the
dishwasher and washing machine filters, and anything SirisOS's maintenance
schedule says is due.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.core.siris import SirisAI

logger = logging.getLogger(__name__)

STATE_KEY = "house:watch"  # entity -> when it first went wrong
_WET = re.compile(r"\b(leak|water|moisture|flood)\b", re.I)
_DOOR = re.compile(r"\b(door|garage|gate)\b", re.I)
HEALTH_LIST = [
    "Test the smoke alarms (press and hold the button)",
    "Clean the air-con and range-hood filters",
    "Clear the dishwasher and washing-machine filters",
    "Check the gutters and downpipes, especially before rain",
    "Run water through any unused drains and taps",
]


def _client(siris: "SirisAI") -> Any:
    return getattr(siris, "_home_assistant_client", None)


def _name(state: dict[str, Any]) -> str:
    return (state.get("attributes") or {}).get("friendly_name") or state["entity_id"].split(".", 1)[1].replace("_", " ")


def _number(state: dict[str, Any]) -> float | None:
    try:
        return float(state.get("state"))
    except (TypeError, ValueError):
        return None


def problems(siris: "SirisAI", states: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    s = siris.settings
    seen: dict[str, str] = dict(siris.state.get(STATE_KEY) or {})
    active: dict[str, str] = {}
    out = []

    def lasting(entity: str, minutes: float) -> float | None:
        """Minutes this has been wrong, once it's been wrong for `minutes`."""
        since = datetime.fromisoformat(seen.get(entity) or now.isoformat())
        active[entity] = since.isoformat()
        held = (now - since).total_seconds() / 60
        return held if held >= minutes else None

    for st in states:
        entity, domain = st["entity_id"], st["entity_id"].split(".", 1)[0]
        attrs = st.get("attributes") or {}
        kind = attrs.get("device_class")
        name = _name(st)
        if domain == "binary_sensor" and st.get("state") == "on" and (kind == "moisture" or (kind is None and _WET.search(name))):
            active[entity] = seen.get(entity) or now.isoformat()
            out.append({"key": f"house:leak:{entity}", "kind": "alert", "severity": "urgent", "push": True,
                        "title": f"Water detected: {name}", "body": "Check for a leak now. Turn the water off at the mains if it's spreading."})
        elif domain == "sensor" and kind == "temperature" and re.search(r"\b(fridge|freezer)\b", name, re.I):
            value = _number(st)
            limit = s.freezer_max_c if re.search(r"freezer", name, re.I) else s.fridge_max_c
            if value is not None and value > limit:
                held = lasting(entity, s.appliance_warm_minutes)
                if held is not None:
                    out.append({"key": f"house:warm:{entity}", "kind": "alert", "severity": "urgent", "push": True,
                                "title": f"{name} is {value:g}°C", "body": f"Above {limit:g}°C for {held:.0f} minutes: check the door's shut, then whether it's running."})
        elif ((domain == "binary_sensor" and kind in ("door", "garage_door", "opening") or (domain == "cover" and kind in ("garage", "gate", "door")))
              and st.get("state") in ("on", "open") and (kind != "opening" or _DOOR.search(name))):
            held = lasting(entity, s.door_open_minutes)
            if held is not None:
                out.append({"key": f"house:open:{entity}", "kind": "alert", "severity": "attention", "push": held < s.door_open_minutes + 5,
                            "title": f"{name} has been open {held:.0f} minutes", "body": "Close it, or dismiss this if it's meant to be open."})
    siris.state.set(STATE_KEY, active)
    return out


def check(siris: "SirisAI", now: datetime | None = None) -> list[dict[str, Any]]:
    client = _client(siris)
    if client is None:
        return []
    now = now or datetime.now(UTC)
    found = problems(siris, client.list_states(), now)
    already = {i["key"] for i in siris.attention.list(status=None, limit=300) if i["status"] in ("open", "dismissed")}
    wanted = []
    for item in found:
        push = item.pop("push", False)
        wanted.append(item)
        if push and item["key"] not in already and siris.tools.get("notify") is not None:
            try:
                siris.run_tool("notify", {"title": item["title"], "message": item["body"], "priority": "urgent" if item["severity"] == "urgent" else "high"},
                               trigger="house watch: a sensor says something's wrong")
            except Exception:  # noqa: BLE001
                logger.exception("House watch push failed")
    siris.attention.sync("house", wanted, outcome="Back to normal")
    return found


def low_batteries(siris: "SirisAI", threshold: float = 20) -> list[str]:
    client = _client(siris)
    if client is None:
        return []
    out = []
    for st in client.list_states(domain="sensor"):
        if (st.get("attributes") or {}).get("device_class") == "battery":
            value = _number(st)
            if value is not None and value < threshold:
                out.append(f"{_name(st)} ({value:.0f}%)")
    return sorted(out)


def monthly(siris: "SirisAI", now: datetime | None = None) -> dict[str, Any]:
    """The first-of-the-month house health checklist."""
    now = now or datetime.now(UTC)
    items = list(HEALTH_LIST)
    batteries = low_batteries(siris)
    if batteries:
        items.append("Replace batteries: " + ", ".join(batteries))
    siris.attention.raise_item(
        f"house:health:{now.strftime('%Y-%m')}", "suggestion", f"House health for {now.strftime('%B')}", source="house",
        severity="info", body="; ".join(items) + ". SirisOS → Maintenance has what's due by date.",
    )
    return {"items": items}


def weekly_batteries(siris: "SirisAI", now: datetime | None = None) -> list[str]:
    batteries = low_batteries(siris)
    wanted = [{"key": "house:batteries", "kind": "suggestion", "severity": "info", "title": f"{len(batteries)} batter{'ies' if len(batteries) != 1 else 'y'} running low",
               "body": ", ".join(batteries) + "."}] if batteries else []
    siris.attention.sync("house-batteries", wanted, outcome="Batteries replaced")
    return batteries
