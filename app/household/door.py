"""Someone's at the door (7.6).

The intruder check (app/skills/builtin/intruder.py) only raises the alarm
for a person seen while nobody's home or at night. In the day, with someone
home, a person at the front door is a visitor: this tells you.

Every 30 s it reads Frigate's person detections on the door cameras
(SIRISAI_DOOR_CAMERAS, or any camera named door, porch, front or doorbell).
For a new one it asks the vision model who's there, then:

- says it on the HUD (as a normal notification, which is spoken at home and
  pushed when you're out) and on the satellite in the room you're in;
- puts it in the feed with the snapshot;
- if they look like a courier and a parcel is out for delivery, says which
  one ("probably your new headphones").

At most once per camera every 3 minutes. When the intruder check is armed
(away or night) it stays out of the way, so you never get two alerts.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.core.siris import SirisAI

logger = logging.getLogger(__name__)
CURSOR_KEY = "door_cursor"
LAST_KEY = "door_last"
COOLDOWN = timedelta(minutes=3)
_DOORISH = re.compile(r"door|porch|front|doorbell|entry", re.I)
_COURIER = re.compile(r"\b(parcel|package|courier|delivery|delivering|box|uniform|post(?:ie|man|woman)?|australia post|dhl|ups|fedex|amazon)\b", re.I)
_EXPECTED = ("OutForDelivery", "AvailableForPickup", "InTransit")


def door_cameras(siris: "SirisAI", cameras: list[str]) -> list[str]:
    chosen = [c.strip() for c in (siris.settings.door_cameras or "").split(",") if c.strip()]
    return chosen or [c for c in cameras if _DOORISH.search(c)]


def _armed(siris: "SirisAI") -> bool:
    from app.protocols import service
    from app.skills.builtin.presence import anyone_home

    mode = siris.settings.intruder_alert_when
    night, _ = service.condition_met(siris, "night")
    away = anyone_home(siris) is False
    return (away and mode in ("away", "away_or_night")) or (night and mode in ("night", "away_or_night"))


def expected_parcel(siris: "SirisAI", description: str) -> dict[str, Any] | None:
    if not _COURIER.search(description or ""):
        return None
    waiting = [p for p in siris.parcels.active() if p.get("status") in _EXPECTED]
    waiting.sort(key=lambda p: _EXPECTED.index(p["status"]))
    return waiting[0] if waiting else None


def check(siris: "SirisAI", now: datetime | None = None) -> list[dict[str, Any]]:
    client = getattr(siris, "_frigate_client", None)
    if client is None or not siris.settings.door_announce:
        return []
    now = now or datetime.now(UTC)
    events = client.recent_events(label="person", limit=15)
    cursor = siris.state.get(CURSOR_KEY)
    newest = max((e["start_time"] for e in events if e.get("start_time")), default=cursor)
    if cursor is None:   # first run: start from now, never announce old visitors
        siris.state.set(CURSOR_KEY, newest or now.isoformat())
        return []
    if newest:
        siris.state.set(CURSOR_KEY, newest)
    cameras = door_cameras(siris, sorted({e.get("camera") or "" for e in events}))
    fresh = [e for e in events if e.get("start_time") and e["start_time"] > cursor and e.get("camera") in cameras]
    if not fresh or _armed(siris):
        return []
    last: dict[str, str] = siris.state.get(LAST_KEY) or {}
    out = []
    for event in sorted(fresh, key=lambda e: e["start_time"]):
        camera = event["camera"]
        if last.get(camera) and now - datetime.fromisoformat(last[camera]) < COOLDOWN:
            continue
        last[camera] = now.isoformat()
        out.append(announce(siris, event, camera))
    siris.state.set(LAST_KEY, last)
    return out


def announce(siris: "SirisAI", event: dict[str, Any], camera: str) -> dict[str, Any]:
    trigger = f"door: a person at the {camera.replace('_', ' ')} camera"
    description = ""
    if event.get("id") and siris.tools.get("frigate_describe_event") is not None:
        try:
            description = siris.run_tool(
                "frigate_describe_event",
                {"event_id": event["id"], "question": "Who is at the door and what are they doing? Say if they carry a parcel or look like a courier. One sentence."},
                trigger=trigger,
            ).get("description") or ""
        except Exception:
            description = ""
    sir = " sir," if (siris.settings.persona_style or "").lower() == "jarvis" else ""
    where = camera.replace("_", " ")
    message = f"Someone's at the {where}" + (f":{sir} {description.strip()}" if description else ".")
    parcel = expected_parcel(siris, description)
    if parcel:
        message += f" Probably your {parcel['label']}" + (f" from {parcel['carrier']}" if parcel.get("carrier") else "") + "."
    spoken = []
    if siris.tools.get("notify") is not None:
        try:
            siris.run_tool("notify", {"title": "At the door", "message": message}, trigger=trigger, evidence=event)
            spoken.append("notified")
        except Exception:
            logger.info("Door notification failed", exc_info=True)
    if siris.tools.get("speak_in_current_room") is not None:
        try:
            siris.run_tool("speak_in_current_room", {"message": message}, trigger=trigger, evidence=event)
            spoken.append("spoke")
        except Exception:
            logger.info("Door announcement failed", exc_info=True)
    siris.attention.raise_item(
        f"door:{event.get('id') or camera}", "alert", message[:200], source="door", severity="info",
        link=f"/siris/cameras/{camera}/latest.jpg", body=description or "",
    )
    return {"camera": camera, "message": message, "parcel": parcel["label"] if parcel else None, "actions": spoken}
