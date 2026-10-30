"""An internet outage log, and a weekly summary to show your ISP.

- **Probing** (every SIRISAI_INTERNET_PROBE_SECONDS, 60): a TCP connection
  to each of SIRISAI_INTERNET_PROBE_TARGETS (Cloudflare, Google and Quad9
  DNS by default). The internet counts as down when none of them answer,
  so one provider having a bad minute isn't an outage. This doesn't depend
  on UniFi or anything else on the LAN.
- **While it's down** for two probes in a row, "The internet is down (since
  14:02)" sits in the attention feed. It closes itself when the connection is
  back, saying how long it was out.
- **Speed** (every SIRISAI_INTERNET_SPEEDTEST_HOURS, 6; 0 = off): a speed
  test, when the speedtest tool is available. A result under half the median
  of the last 30 days counts as a dip.
- **Weekly** (Mondays at SIRISAI_INTERNET_REPORT_HOUR_LOCAL, 8am): "Internet
  last week: 3 drops, 41 minutes in total, the worst Tue 02:00 for 25 minutes.
  Speed averaged 480/38 Mbps with 2 dips." in the feed.
  `internet_outages` gives the same for any number of days.

Outages and speed results are kept for 90 days. Time SirisAI itself was
stopped isn't counted either way.
"""

import logging
import socket
import statistics
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from app.core import clock

if TYPE_CHECKING:
    from app.core.siris import SirisAI

logger = logging.getLogger(__name__)

STATE_KEY = "internet_log"
KEEP = timedelta(days=90)
DOWN_ALERT_AFTER = 2


def _local(siris: "SirisAI", when: datetime) -> datetime:
    return when.astimezone(clock.get_zone(siris.settings.timezone))


def targets(siris: "SirisAI") -> list[tuple[str, int]]:
    out = []
    for part in siris.settings.internet_probe_targets.split(","):
        host, _, port = part.strip().rpartition(":")
        if host and port.isdigit():
            out.append((host.strip("[]"), int(port)))
    return out


def _reachable(target: tuple[str, int], timeout: float) -> bool:
    try:
        with socket.create_connection(target, timeout=timeout):
            return True
    except OSError:
        return False


def online(siris: "SirisAI", timeout: float = 3.0) -> bool:
    found = targets(siris)
    if not found:
        return True
    with ThreadPoolExecutor(max_workers=len(found)) as pool:
        return any(pool.map(lambda t: _reachable(t, timeout), found))


def _state(siris: "SirisAI") -> dict[str, Any]:
    state = dict(siris.state.get(STATE_KEY) or {})
    state.setdefault("outages", [])
    state.setdefault("speeds", [])
    return state


def probe(siris: "SirisAI", now: datetime | None = None, up: bool | None = None) -> dict[str, Any]:
    """One probe. Opens an outage on the first failure; closes it, logged,
    when the connection's back."""
    now = now or datetime.now(UTC)
    up = online(siris) if up is None else up
    state = _state(siris)
    down = state.get("down")
    last = state.get("last_probe")
    # A gap much longer than the probe interval means SirisAI wasn't running:
    # an outage open from before then ends at the last probe it saw.
    gap = (now - datetime.fromisoformat(last)).total_seconds() if last else 0
    if down and gap > max(5 * siris.settings.internet_probe_seconds, 300):
        _close(siris, state, down, datetime.fromisoformat(last), cut_short=True)
        down = None
    if up and down:
        _close(siris, state, down, now)
    elif not up:
        if not down:
            down = state["down"] = {"start": now.isoformat(), "failed": 0}
        down["failed"] = int(down.get("failed", 0)) + 1
        if down["failed"] == DOWN_ALERT_AFTER:
            local = _local(siris, datetime.fromisoformat(down["start"]))
            siris.attention.raise_item(
                "internet:down",
                "alert",
                f"The internet is down (since {local:%H:%M})",
                source="internet",
                severity="urgent",
                body="None of " + ", ".join(f"{h}:{p}" for h, p in targets(siris)) + " are answering. It'll be logged for the weekly summary.",
            )
    state["last_probe"] = now.isoformat()
    siris.state.set(STATE_KEY, state)
    return {"online": up, "down_since": (state.get("down") or {}).get("start")}


def _close(siris: "SirisAI", state: dict[str, Any], down: dict[str, Any], end: datetime, cut_short: bool = False) -> None:
    start = datetime.fromisoformat(down["start"])
    seconds = max((end - start).total_seconds(), float(siris.settings.internet_probe_seconds))
    state["outages"] = _trim([*state["outages"], {"start": start.isoformat(), "end": end.isoformat(), "seconds": round(seconds)}], end)
    state.pop("down", None)
    for item in siris.attention.list("open", limit=500):
        if item["key"] == "internet:down":
            outcome = "SirisAI stopped while it was down" if cut_short else f"Back after {_duration(seconds)}"
            siris.attention.close(item["id"], "done", outcome)


def _trim(entries: list[dict[str, Any]], now: datetime, key: str = "start") -> list[dict[str, Any]]:
    return [e for e in entries if now - datetime.fromisoformat(e[key]) <= KEEP]


def _duration(seconds: float) -> str:
    minutes = round(seconds / 60)
    if minutes < 1:
        return "under a minute"
    if minutes < 90:
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    return f"{minutes / 60:.1f} hours"


def speed_test(siris: "SirisAI", now: datetime | None = None) -> dict[str, Any] | None:
    if siris.tools.get("run_speed_test") is None:
        return None
    now = now or datetime.now(UTC)
    try:
        result = siris.run_tool("run_speed_test", {}, trigger="internet log: a scheduled speed test")
    except Exception as exc:  # noqa: BLE001 - a failed test is just a missing sample
        logger.warning("Scheduled speed test failed: %s", exc)
        return None
    sample = {"at": now.isoformat(), "down": result.get("download_mbps"), "up": result.get("upload_mbps"), "ping": result.get("ping_ms")}
    state = _state(siris)
    state["speeds"] = _trim([*state["speeds"], sample], now, key="at")
    siris.state.set(STATE_KEY, state)
    return sample


def summary(siris: "SirisAI", days: float = 7, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    since = now - timedelta(days=days)
    state = _state(siris)
    outages = [o for o in state["outages"] if datetime.fromisoformat(o["end"]) >= since]
    total = sum(o["seconds"] for o in outages)
    worst = max(outages, key=lambda o: o["seconds"], default=None)
    speeds = [s for s in state["speeds"] if s.get("down") and datetime.fromisoformat(s["at"]) >= since]
    month = [s["down"] for s in state["speeds"] if s.get("down") and datetime.fromisoformat(s["at"]) >= now - timedelta(days=30)]
    median = statistics.median(month) if month else None
    dips = [s for s in speeds if median and s["down"] < median / 2]
    out: dict[str, Any] = {
        "days": days,
        "drops": len(outages),
        "minutes_down": round(total / 60, 1),
        "worst": worst,
        "outages": outages,
        "speed_tests": len(speeds),
        "average_down_mbps": round(statistics.mean(s["down"] for s in speeds), 1) if speeds else None,
        "average_up_mbps": round(statistics.mean(s["up"] for s in speeds if s.get("up")), 1) if any(s.get("up") for s in speeds) else None,
        "dips": dips,
        "down_now_since": (state.get("down") or {}).get("start"),
    }
    out["text"] = _sentence(siris, out)
    return out


def _sentence(siris: "SirisAI", s: dict[str, Any]) -> str:
    period = "last week" if s["days"] == 7 else f"over the last {s['days']:g} days"
    if not s["drops"]:
        text = f"Internet {period}: no drops."
    else:
        worst = s["worst"]
        local = _local(siris, datetime.fromisoformat(worst["start"]))
        text = (
            f"Internet {period}: {s['drops']} drop{'s' if s['drops'] != 1 else ''}, {_duration(s['minutes_down'] * 60)} in total"
            + (f", the worst {local:%a %H:%M} for {_duration(worst['seconds'])}." if s["drops"] > 1 else f", {local:%a %H:%M}.")
        )
    if s["average_down_mbps"] is not None:
        up = f"/{s['average_up_mbps']:.0f}" if s["average_up_mbps"] is not None else ""
        dips = len(s["dips"])
        text += f" Speed averaged {s['average_down_mbps']:.0f}{up} Mbps" + (f" with {dips} dip{'s' if dips != 1 else ''} below half the usual." if dips else ".")
    return text


def weekly(siris: "SirisAI", now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    found = summary(siris, 7, now)
    lines = [found["text"]]
    for o in found["outages"][:15]:
        local = _local(siris, datetime.fromisoformat(o["start"]))
        lines.append(f"- {local:%a %d %b %H:%M}: {_duration(o['seconds'])}")
    siris.attention.raise_item(
        f"internet:weekly:{now:%G-W%V}",
        "alert",
        found["text"],
        source="internet-weekly",
        severity="attention" if found["minutes_down"] >= 30 or found["dips"] else "info",
        body="\n".join(lines),
    )
    return found
