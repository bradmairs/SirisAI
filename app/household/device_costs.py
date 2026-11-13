"""What each device costs (6.48): energy_by_device.

From Home Assistant's energy sensors (the ones the weekly energy report
uses): each device's kWh over the last N days (30) and what that comes to
a month, at the price sensor's average over the period or
SIRISAI_ENERGY_FLAT_RATE. From the power sensors: what each draws overnight
(its standby, as a monthly cost) and what it drew while everyone was out
(from away mode's record of when the house was empty), so things left on
stand out: "Office PC: 85 W while you were out, about $14 a month".
"""

from __future__ import annotations

import statistics
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from app.household import energy

if TYPE_CHECKING:
    from app.core.siris import SirisAI

MIN_AWAY_HOURS = 1.0


def _average(points: list[tuple[datetime, float]], start: datetime, end: datetime) -> tuple[float, float]:
    """(watt-seconds, seconds covered) between start and end, holding each reading until the next."""
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
    return total, covered


def report(siris: "SirisAI", days: int = 30, now: datetime | None = None) -> dict[str, Any]:
    from app.household import away

    now = now or datetime.now(UTC)
    if getattr(siris, "_home_assistant_client", None) is None:
        raise RuntimeError("Home Assistant isn't configured.")
    found = energy.sensors(siris)
    start = now - timedelta(days=days)
    price_id = siris.settings.energy_price_entity_id
    ids = [s["entity_id"] for s in found["energy"] + found["power"]] + ([price_id] if price_id else [])
    history = siris._home_assistant_client.get_history_many(ids, start - timedelta(hours=6), now)  # type: ignore[union-attr]
    series = {k: energy._numbers(v) for k, v in history.items()}
    prices = [v for t, v in series.get(price_id, []) if t >= start] if price_id else []
    rate = statistics.mean(prices) if prices else (siris.settings.energy_flat_rate or None)
    month = 30 / max(days, 1)
    cur = siris.settings.energy_currency

    devices = []
    for s in found["energy"]:
        if s["entity_id"] == siris.settings.energy_meter_entity_id:
            continue
        kwh = sum(d for _, d in energy.deltas(series.get(s["entity_id"], []), start, now)) * s["scale"]
        if kwh > 0:
            devices.append({"entity_id": s["entity_id"], "name": s["name"], "kwh": round(kwh, 2),
                            "kwh_month": round(kwh * month, 1), "cost_month": round(kwh * month * rate, 2) if rate else None})
    devices.sort(key=lambda d: -d["kwh"])

    away_spans = [(a, b) for a, b in away.away_periods(siris, now) if b > start and (b - max(a, start)).total_seconds() >= MIN_AWAY_HOURS * 3600]
    threshold = siris.settings.energy_overnight_watts
    standby, while_away = [], []
    for s in found["power"]:
        points = series.get(s["entity_id"], [])
        nights = energy.overnight(siris, points, s["scale"], now)
        if len(nights) >= 3 and statistics.median(nights) > threshold:
            watts = statistics.median(nights)
            kwh_month = watts * 24 * 30 / 1000
            standby.append({"entity_id": s["entity_id"], "name": s["name"], "watts": round(watts),
                            "cost_month_if_always": round(kwh_month * rate, 2) if rate else None})
        total = covered = 0.0
        for a, b in away_spans:
            ws, secs = _average(points, max(a, start), b)
            total, covered = total + ws, covered + secs
        if covered >= MIN_AWAY_HOURS * 3600 and total / covered * s["scale"] > threshold:
            watts = total / covered * s["scale"]
            while_away.append({"entity_id": s["entity_id"], "name": s["name"], "watts": round(watts), "hours": round(covered / 3600, 1),
                               "cost": round(watts * covered / 3600 / 1000 * rate, 2) if rate else None})
    standby.sort(key=lambda d: -d["watts"])
    while_away.sort(key=lambda d: -d["watts"])

    def money(x: float | None) -> str:
        return f" ({cur}{x:,.2f})" if x is not None else ""

    parts = []
    if devices:
        parts.append(f"Over the last {days} days, by device: " + ", ".join(
            f"{d['name']} {d['kwh']:g} kWh" + (f", about {cur}{d['cost_month']:,.2f} a month" if d["cost_month"] is not None else "")
            for d in devices[:6]) + ".")
    if while_away:
        parts.append("On while everyone was out: " + ", ".join(
            f"{d['name']} about {d['watts']} W for {d['hours']:g} h{money(d['cost'])}" for d in while_away[:4]) + ".")
    if standby:
        parts.append("Drawing power overnight: " + ", ".join(
            f"{d['name']} about {d['watts']} W" + (f" (about {cur}{d['cost_month_if_always']:,.2f} a month if always on)" if d["cost_month_if_always"] else "")
            for d in standby[:4]) + ".")
    if not parts:
        parts.append("No per-device energy or power sensors with data in Home Assistant.")
    if rate is None:
        parts.append("Set SIRISAI_ENERGY_FLAT_RATE (or a price sensor) to see costs.")
    return {"days": days, "rate": rate, "devices": devices, "while_away": while_away, "standby": standby, "text": " ".join(parts)}
