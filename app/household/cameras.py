"""A camera digest: what Frigate saw since the last one, in one message
instead of an alert per motion event.

At each of SIRISAI_CAMERA_DIGEST_HOURS (local hours, "12,18,22" by default),
the events since the previous digest are summarised per camera:

    Front door: 2 people (a courier at 11:04), 1 package. Driveway: 1 car.

Couriers are recognised from Frigate+'s courier labels (ups, fedex, amazon,
dhl, auspost...) and from sub-labels; a recognised face's name is used.
Frigate's own description (with its generative AI on) is listed under the
summary. The digest goes to the attention feed and, with
SIRISAI_CAMERA_DIGEST_PUSH (on), to your phone. Nothing seen means no digest.

Turn off Frigate's or Home Assistant's own per-event notifications to get
the benefit; the intruder alert (a person while nobody's home) still alerts
at once.
"""

import logging
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from app.core import clock

if TYPE_CHECKING:
    from app.core.siris import SirisAI

logger = logging.getLogger(__name__)

STATE_KEY = "camera_digest"
COURIERS = {
    "ups", "fedex", "amazon", "dhl", "usps", "auspost", "australia_post", "an_post", "purolator", "postnl",
    "nzpost", "postnord", "gls", "dpd", "royal_mail", "canada_post", "startrack", "courier",
}
PLURAL = {"person": "people", "package": "packages", "bus": "buses"}


def hours(siris: "SirisAI") -> list[int]:
    return sorted({int(h) for h in siris.settings.camera_digest_hours.split(",") if h.strip().isdigit() and 0 <= int(h) < 24})


def due(siris: "SirisAI", now: datetime | None = None) -> bool:
    now = now or datetime.now(UTC)
    wanted = hours(siris)
    if not wanted or getattr(siris, "_frigate_client", None) is None:
        return False
    local = now.astimezone(clock.get_zone(siris.settings.timezone))
    passed = [h for h in wanted if h <= local.hour]
    if not passed:
        return False
    slot = local.replace(hour=passed[-1], minute=0, second=0, microsecond=0).astimezone(UTC)
    last = (siris.state.get(STATE_KEY) or {}).get("last")
    return last is None or datetime.fromisoformat(last) < slot


def _count(label: str, n: int) -> str:
    word = PLURAL.get(label, label + "s") if n != 1 else label
    return f"{n} {word}"


def summarise(siris: "SirisAI", events: list[dict[str, Any]]) -> tuple[str, list[str]]:
    zone = clock.get_zone(siris.settings.timezone)
    by_camera: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for e in events:
        by_camera[e.get("camera") or "camera"].append(e)
    parts, notes = [], []
    for camera, seen in sorted(by_camera.items(), key=lambda kv: -len(kv[1])):
        counts: Counter[str] = Counter()
        extras: list[str] = []
        for e in seen:
            label = (e.get("label") or "object").lower()
            sub = str(e.get("sub_label") or "").strip()
            when = datetime.fromisoformat(e["start_time"]).astimezone(zone).strftime("%H:%M") if e.get("start_time") else "?"
            if label in COURIERS or sub.lower() in COURIERS:
                counts["person"] += 1
                extras.append(f"a courier at {when}")
            elif label == "person" and sub:
                counts["person"] += 1
                extras.append(f"{sub} at {when}")
            else:
                counts[label] += 1
            if e.get("description"):
                notes.append(f"{camera.replace('_', ' ').title()} {when}: {e['description'][:200]}")
        detail = f" ({', '.join(extras[:3])}{' and more' if len(extras) > 3 else ''})" if extras else ""
        listed = ", ".join(_count(label, n) + (detail if label == "person" else "") for label, n in counts.most_common())
        parts.append(f"{camera.replace('_', ' ').title()}: {listed}.")
    return " ".join(parts), notes


def digest(siris: "SirisAI", now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    state = dict(siris.state.get(STATE_KEY) or {})
    since = datetime.fromisoformat(state["last"]) if state.get("last") else now - timedelta(hours=12)
    events = siris._frigate_client.events_since(since)  # type: ignore[union-attr]
    state["last"] = now.isoformat()
    siris.state.set(STATE_KEY, state)
    if not events:
        return {"events": 0}
    text, notes = summarise(siris, events)
    local_since = since.astimezone(clock.get_zone(siris.settings.timezone))
    body = f"Since {local_since:%H:%M}" + (f" {local_since:%a}" if (now - since) > timedelta(hours=20) else "") + f": {text}"
    if notes:
        body += "\n\n" + "\n".join(notes[:10])
    siris.attention.raise_item(
        f"cameras:digest:{now:%Y-%m-%dT%H}",
        "alert",
        text if len(text) <= 120 else text[:117] + "...",
        source="cameras",
        severity="info",
        body=body,
    )
    if siris.settings.camera_digest_push and siris.tools.get("notify") is not None:
        try:
            siris.run_tool("notify", {"title": "Cameras", "message": body[:900]}, trigger="camera digest")
        except Exception:  # noqa: BLE001 - the feed has it anyway
            logger.exception("Pushing the camera digest failed")
    return {"events": len(events), "text": text}
