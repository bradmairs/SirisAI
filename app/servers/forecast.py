"""When each disk fills up, at the rate it's filling (6.48).

The health check (app/servers/watch.py) now also records each mount's
used and total space (`df -kP`) once a day per server, kept for 60 days. A
straight-line fit over the last weeks gives the growth per day; a disk that
will be full within SIRISAI_DISK_FORECAST_DAYS (30) is raised early ("The
media disk on nas fills in about 3 weeks at this rate"), long before the
90% warning. disk_forecast lists them all.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.core.siris import SirisAI

STATE_KEY = "server:disk_history"
KEEP_DAYS = 60
MIN_POINTS = 5
MIN_SPAN_DAYS = 6
_UNITS = {"K": 1, "M": 1024, "G": 1024**2, "T": 1024**3, "P": 1024**4}


def _kb(text: str) -> float | None:
    m = re.fullmatch(r"([\d.]+)([KMGTP])?i?B?", (text or "").strip(), re.I)
    if not m:
        return None
    return float(m.group(1)) * _UNITS.get((m.group(2) or "K").upper(), 1)


def parse_dfk(stdout: str) -> dict[str, tuple[float, float]]:
    """{mount: (used_kb, total_kb)} from the ##dfk section, if the health command had one."""
    out, inside = {}, False
    for line in stdout.splitlines():
        if line.startswith("##"):
            inside = line.strip() == "##dfk"
            continue
        fields = line.split()
        if inside and len(fields) >= 6 and fields[1].isdigit() and fields[2].isdigit():
            out[fields[5]] = (float(fields[2]), float(fields[1]))
    return out


def record(siris: "SirisAI", server: str, health: dict[str, Any], stdout: str = "", now: datetime | None = None) -> None:
    day = (now or datetime.now(UTC)).date().isoformat()
    exact = parse_dfk(stdout)
    data = dict(siris.state.get(STATE_KEY) or {})
    mounts = dict(data.get(server) or {})
    for disk in health.get("disks") or []:
        mount = disk["mount"]
        if mount in exact:
            used, total = exact[mount]
        else:
            total = _kb(disk.get("size", ""))
            free = _kb(disk.get("free", ""))
            if not total or free is None:
                continue
            used = total - free
        points = [p for p in mounts.get(mount) or [] if p[0] != day]
        points.append([day, used, total])
        cutoff = date.fromisoformat(day).toordinal() - KEEP_DAYS
        mounts[mount] = [p for p in points if date.fromisoformat(p[0]).toordinal() > cutoff][-KEEP_DAYS:]
    data[server] = mounts
    siris.state.set(STATE_KEY, data)


def fit(points: list[list[Any]]) -> dict[str, Any] | None:
    """Growth per day (kB) and days until full, from [[day, used_kb, total_kb], ...]."""
    if len(points) < MIN_POINTS:
        return None
    xs = [date.fromisoformat(p[0]).toordinal() for p in points]
    if xs[-1] - xs[0] < MIN_SPAN_DAYS:
        return None
    ys = [float(p[1]) for p in points]
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    var = sum((x - mx) ** 2 for x in xs)
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / var if var else 0.0
    used, total = ys[-1], float(points[-1][2])
    free = max(0.0, total - used)
    days = free / slope if slope > 0 else None
    return {"per_day_kb": slope, "free_kb": free, "total_kb": total, "used_percent": round(100 * used / total) if total else None,
            "days_to_full": round(days, 1) if days is not None else None}


def _size(kb: float) -> str:
    for unit, scale in (("TB", 1024**3), ("GB", 1024**2), ("MB", 1024)):
        if abs(kb) >= scale:
            return f"{kb / scale:.1f} {unit}"
    return f"{kb:.0f} kB"


def _when(days: float) -> str:
    if days < 2:
        return "within a couple of days"
    if days < 14:
        return f"in about {round(days)} days"
    return f"in about {round(days / 7)} weeks"


def forecasts(siris: "SirisAI") -> list[dict[str, Any]]:
    out = []
    for server, mounts in (siris.state.get(STATE_KEY) or {}).items():
        for mount, points in mounts.items():
            f = fit(points)
            if f:
                out.append({"server": server, "mount": mount, **f})
    return sorted(out, key=lambda f: f["days_to_full"] if f["days_to_full"] is not None else 1e9)


def wanted(siris: "SirisAI", warn_percent: int) -> list[dict[str, Any]]:
    from app.hub.attention import action

    items = []
    for f in forecasts(siris):
        days = f["days_to_full"]
        if days is None or days > siris.settings.disk_forecast_days or (f["used_percent"] or 0) >= warn_percent:
            continue  # not soon, or the 90% warning already covers it
        items.append({
            "key": f"servers:diskfill:{f['server']}:{f['mount']}", "kind": "alert",
            "severity": "attention" if days > 7 else "urgent",
            "title": f"{f['mount']} on {f['server']} fills {_when(days)} at this rate",
            "body": f"It's growing about {_size(f['per_day_kb'])} a day with {_size(f['free_kb'])} free of {_size(f['total_kb'])}.",
            "actions": [action("acknowledge", "Got it")], "payload": {"server": f["server"]},
        })
    return items


def text(siris: "SirisAI") -> str:
    found = forecasts(siris)
    if not found:
        return "Not enough history yet: it takes about a week of daily checks."
    parts = []
    for f in found:
        when = _when(f["days_to_full"]) if f["days_to_full"] is not None else "not filling"
        parts.append(f"{f['mount']} on {f['server']}: {f['used_percent']}% used, {_size(f['per_day_kb'])} a day, full {when}"
                     if f["days_to_full"] is not None else f"{f['mount']} on {f['server']}: {f['used_percent']}% used, not growing")
    return "; ".join(parts) + "."
