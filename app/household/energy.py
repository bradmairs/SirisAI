"""A weekly energy report from Home Assistant's energy and power sensors
(Mondays at SIRISAI_ENERGY_REPORT_HOUR_LOCAL, 8am; `energy_report` any time).

    Energy last week: 142 kWh ($45.20), 12% more than the week before.
    Biggest: Pool pump 38 kWh, Heat pump 31 kWh, EV charger 22 kWh.
    Left on overnight: Office PC (5 nights, about 85 W).

- **Use** comes from the energy sensors (kWh meters that only count up,
  resets handled). The total is SIRISAI_ENERGY_METER_ENTITY_ID (the house's
  kWh meter, also used by SirisOS's power view) when set, otherwise the
  devices added up.
  Solar, export and return meters are left out of both.
- **Cost** uses the price sensor (SIRISAI_ENERGY_PRICE_ENTITY_ID, already
  used for cheap-power scheduling) at the time each kWh was used, or the
  flat SIRISAI_ENERGY_FLAT_RATE, or is left out.
- **Left on overnight**: power sensors averaging over
  SIRISAI_ENERGY_OVERNIGHT_WATTS (25 W) between midnight and 5am on at least
  3 of the last 7 nights. Leave the fridge and the like out with
  SIRISAI_ENERGY_IGNORE.
"""

import logging
import statistics
from datetime import UTC, datetime, time, timedelta
from typing import TYPE_CHECKING, Any

from app.core import clock

if TYPE_CHECKING:
    from app.core.siris import SirisAI

logger = logging.getLogger(__name__)

NOT_USE = ("solar", "export", "production", "return", "feed_in", "generation")
UNITS = {"wh": 0.001, "kwh": 1.0, "mwh": 1000.0}
POWER_UNITS = {"w": 1.0, "kw": 1000.0}


def _ignored(siris: "SirisAI") -> set[str]:
    return {x.strip().lower() for x in siris.settings.energy_ignore.split(",") if x.strip()}


def sensors(siris: "SirisAI") -> dict[str, list[dict[str, Any]]]:
    """{"energy": [...], "power": [...]}: id, name and unit scale of each."""
    ignored = _ignored(siris)
    found: dict[str, list[dict[str, Any]]] = {"energy": [], "power": []}
    for state in siris._home_assistant_client.list_states("sensor"):  # type: ignore[union-attr]
        attrs = state.get("attributes") or {}
        entity = state["entity_id"]
        name = attrs.get("friendly_name") or entity.split(".", 1)[-1].replace("_", " ")
        if entity.lower() in ignored or name.lower() in ignored:
            continue
        unit = str(attrs.get("unit_of_measurement") or "").lower()
        if attrs.get("device_class") == "energy" and unit in UNITS and attrs.get("state_class") in ("total_increasing", "total"):
            if any(word in entity.lower() or word in name.lower() for word in NOT_USE):
                continue
            found["energy"].append({"entity_id": entity, "name": name, "scale": UNITS[unit]})
        elif attrs.get("device_class") == "power" and unit in POWER_UNITS:
            if any(word in entity.lower() or word in name.lower() for word in NOT_USE):
                continue
            found["power"].append({"entity_id": entity, "name": name, "scale": POWER_UNITS[unit]})
    return found


def _numbers(points: list[tuple[datetime, str]]) -> list[tuple[datetime, float]]:
    out = []
    for when, value in points:
        try:
            out.append((when, float(value)))
        except ValueError:
            continue  # unavailable / unknown
    return out


def deltas(points: list[tuple[datetime, float]], start: datetime, end: datetime) -> list[tuple[datetime, float]]:
    """Each increase of a counting-up meter inside (start, end]; a drop is a
    reset, so the new reading counts from zero."""
    before = [v for t, v in points if t <= start]
    previous = before[-1] if before else None
    out = []
    for when, value in points:
        if when <= start or when > end:
            continue
        if previous is not None:
            out.append((when, value - previous if value >= previous else value))
        previous = value
    return out


def _price_at(prices: list[tuple[datetime, float]], when: datetime) -> float | None:
    found = None
    for t, v in prices:
        if t > when:
            break
        found = v
    return found if found is not None else (prices[0][1] if prices else None)


def overnight(siris: "SirisAI", points: list[tuple[datetime, float]], scale: float, now: datetime) -> list[float]:
    """Average watts between midnight and 5am local, for each of the last 7 nights."""
    zone = clock.get_zone(siris.settings.timezone)
    today = now.astimezone(zone).date()
    nights = []
    for back in range(1, 8):
        day = today - timedelta(days=back - 1)
        start = datetime.combine(day, time(0, 0), zone).astimezone(UTC)
        end = datetime.combine(day, time(5, 0), zone).astimezone(UTC)
        if end > now:
            continue
        before = [v for t, v in points if t <= start]
        level = before[-1] if before else None
        cursor, total, covered = start, 0.0, 0.0
        for t, v in points:
            if t <= start or t >= end:
                continue
            if level is not None:
                total += level * (t - cursor).total_seconds()
                covered += (t - cursor).total_seconds()
            cursor, level = t, v
        if level is not None:
            total += level * (end - cursor).total_seconds()
            covered += (end - cursor).total_seconds()
        if covered >= 3600:
            nights.append(total / covered * scale)
    return nights


def report(siris: "SirisAI", days: int = 7, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    if getattr(siris, "_home_assistant_client", None) is None:
        raise RuntimeError("Home Assistant isn't configured.")
    found = sensors(siris)
    total_id = siris.settings.energy_meter_entity_id
    price_id = siris.settings.energy_price_entity_id
    week, prior = now - timedelta(days=days), now - timedelta(days=2 * days)
    ids = [s["entity_id"] for s in found["energy"]] + [s["entity_id"] for s in found["power"]]
    ids += [x for x in (total_id, price_id) if x and x not in ids]
    history = siris._home_assistant_client.get_history_many(ids, prior - timedelta(hours=6), now)  # type: ignore[union-attr]
    series = {k: _numbers(v) for k, v in history.items()}

    devices = []
    for s in found["energy"]:
        if s["entity_id"] == total_id:
            continue
        points = series.get(s["entity_id"], [])
        used = sum(d for _, d in deltas(points, week, now)) * s["scale"]
        before = sum(d for _, d in deltas(points, prior, week)) * s["scale"]
        if used > 0 or before > 0:
            devices.append({"entity_id": s["entity_id"], "name": s["name"], "kwh": round(used, 2), "kwh_before": round(before, 2)})
    devices.sort(key=lambda d: -d["kwh"])

    if total_id:
        scale = next((s["scale"] for s in found["energy"] if s["entity_id"] == total_id), 1.0)
        total_deltas = [(t, d * scale) for t, d in deltas(series.get(total_id, []), week, now)]
        total_before = sum(d for _, d in deltas(series.get(total_id, []), prior, week)) * scale
    else:
        total_deltas = [(t, d * s["scale"]) for s in found["energy"] for t, d in deltas(series.get(s["entity_id"], []), week, now)]
        total_before = sum(d["kwh_before"] for d in devices)
    total = sum(d for _, d in total_deltas)

    prices = series.get(price_id, []) if price_id else []
    if prices:
        cost: float | None = sum(d * (_price_at(prices, t) or 0) for t, d in total_deltas)
    elif siris.settings.energy_flat_rate > 0:
        cost = total * siris.settings.energy_flat_rate
    else:
        cost = None

    left_on = []
    threshold = siris.settings.energy_overnight_watts
    for s in found["power"]:
        nights = overnight(siris, series.get(s["entity_id"], []), s["scale"], now)
        high = [w for w in nights if w > threshold]
        if len(high) >= 3:
            left_on.append({"entity_id": s["entity_id"], "name": s["name"], "nights": len(high), "watts": round(statistics.median(high))})
    left_on.sort(key=lambda d: -d["watts"])

    out = {
        "days": days,
        "kwh": round(total, 1),
        "kwh_before": round(total_before, 1),
        "change_percent": round((total - total_before) / total_before * 100) if total_before > 0 else None,
        "cost": round(cost, 2) if cost is not None else None,
        "biggest": devices[:5],
        "left_on_overnight": left_on[:5],
        "sensors": {"energy": len(found["energy"]), "power": len(found["power"])},
    }
    out["text"] = sentence(siris, out)
    return out


def sentence(siris: "SirisAI", r: dict[str, Any]) -> str:
    period = "last week" if r["days"] == 7 else f"over the last {r['days']} days"
    if not r["kwh"] and not r["biggest"]:
        return f"No energy use recorded {period} (no energy sensors with data in Home Assistant)."
    money = f" ({siris.settings.energy_currency}{r['cost']:,.2f})" if r["cost"] is not None else ""
    text = f"Energy {period}: {r['kwh']:g} kWh{money}"
    change = r["change_percent"]
    if change is not None:
        text += ", the same as the week before." if change == 0 else f", {abs(change)}% {'more' if change > 0 else 'less'} than the week before."
    else:
        text += "."
    if r["biggest"]:
        text += " Biggest: " + ", ".join(f"{d['name']} {d['kwh']:g} kWh" for d in r["biggest"][:3]) + "."
    if r["left_on_overnight"]:
        text += " Left on overnight: " + ", ".join(f"{d['name']} ({d['nights']} nights, about {d['watts']} W)" for d in r["left_on_overnight"][:3]) + "."
    return text


def weekly(siris: "SirisAI", now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    found = report(siris, 7, now)
    lines = [found["text"]]
    for d in found["biggest"]:
        before = f" (was {d['kwh_before']:g})" if d["kwh_before"] else ""
        lines.append(f"- {d['name']}: {d['kwh']:g} kWh{before}")
    siris.attention.raise_item(
        f"energy:weekly:{now:%G-W%V}",
        "alert",
        found["text"][:300],
        source="energy-weekly",
        severity="attention" if found["left_on_overnight"] or (found["change_percent"] or 0) >= 25 else "info",
        body="\n".join(lines),
    )
    return found
