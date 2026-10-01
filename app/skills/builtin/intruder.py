"""Intruder response: a person on camera while nobody is home, or at night,
raises the alarm without waiting for anyone to ask.

Runs on the background scheduler every SIRISAI_INTRUDER_CHECK_INTERVAL_MINUTES.
Reads Frigate's recent person detections directly (a background look, not a
decision, so it doesn't fill the audit log every minute); only what it *does*
about one goes through run_tool and is audited: a vision-model description of
the snapshot when available, an urgent push, a spoken warning in the room
someone is in (at night, when people are home), and -- only if
SIRISAI_INTRUDER_AUTO_LOCKDOWN is set -- the lockdown protocol.

The first run just records where Frigate's event list is up to, so old
detections are never alerted on. Each camera alerts at most once per
SIRISAI_INTRUDER_COOLDOWN_MINUTES.
"""

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from app.skills.base import Skill

if TYPE_CHECKING:
    from app.core.siris import SirisAI

CURSOR_KEY = "intruder_cursor"
LAST_ALERT_KEY = "intruder_last_alert"


class IntruderCheckSkill(Skill):
    name = "intruder_check"
    description = (
        "Check cameras for people seen while nobody is home or at night, and if so push an urgent alert "
        "(with what the camera saw), warn whoever is home, and optionally run the lockdown protocol."
    )
    parameters = {"type": "object", "properties": {}, "required": []}

    def run(self, siris: "SirisAI", now: datetime | None = None, **kwargs: Any) -> Any:
        from app.protocols import service
        from app.skills.builtin.presence import anyone_home

        client = siris._frigate_client
        if client is None:
            return {"checked": 0, "alerts": []}
        now = now or datetime.now(UTC)
        events = client.recent_events(label="person", limit=25)
        cursor = siris.state.get(CURSOR_KEY)
        newest = max((e["start_time"] for e in events if e.get("start_time")), default=cursor)
        if cursor is None:
            siris.state.set(CURSOR_KEY, newest or now.isoformat())
            return {"checked": len(events), "alerts": [], "baseline": True}
        fresh = [e for e in events if e.get("start_time") and e["start_time"] > cursor]
        if newest:
            siris.state.set(CURSOR_KEY, newest)

        cameras = siris.settings.intruder_camera_list()
        fresh = [e for e in fresh if not cameras or e.get("camera") in cameras]
        home = anyone_home(siris)
        night, _ = service.condition_met(siris, "night")
        away = home is False
        mode = siris.settings.intruder_alert_when
        armed = (away and mode in ("away", "away_or_night")) or (night and mode in ("night", "away_or_night"))
        if not fresh or not armed:
            return {"checked": len(fresh), "alerts": [], "armed": armed}

        last_alert: dict[str, str] = siris.state.get(LAST_ALERT_KEY) or {}
        cooldown = timedelta(minutes=siris.settings.intruder_cooldown_minutes)
        alerts = []
        for event in sorted(fresh, key=lambda e: e["start_time"]):
            camera = event.get("camera") or "a camera"
            previous = last_alert.get(camera)
            if previous and now - datetime.fromisoformat(previous) < cooldown:
                continue
            last_alert[camera] = now.isoformat()
            alerts.append(self._escalate(siris, event, camera, away=away, night=night, home=home))
        siris.state.set(LAST_ALERT_KEY, last_alert)
        return {"checked": len(fresh), "alerts": alerts, "armed": True}

    def _escalate(self, siris: "SirisAI", event: dict[str, Any], camera: str, *, away: bool, night: bool, home: bool | None) -> dict[str, Any]:
        from app.protocols import service

        trigger = "intruder_check: person detected while " + ("nobody was home" if away else "it was night")
        camera_name = camera.replace("_", " ")
        description = None
        if event.get("id") and siris.tools.get("frigate_describe_event") is not None:
            try:
                description = siris.run_tool(
                    "frigate_describe_event",
                    {"event_id": event["id"], "question": "Describe the person and what they are doing."},
                    trigger=trigger,
                )["description"]
            except Exception:
                description = None
        situation = "nobody is home" if away else "it's night"
        message = f"Person seen on the {camera_name} camera while {situation}."
        if description:
            message += f" {description}"
        actions: list[str] = []
        if siris.tools.get("notify") is not None:
            siris.run_tool("notify", {"title": "SirisAI: person detected", "message": message, "priority": "urgent"}, trigger=trigger, evidence=event)
            actions.append("notified")
        if night and home and siris.tools.get("speak_in_current_room") is not None:
            siris.run_tool("speak_in_current_room", {"message": f"Heads up: someone is outside, on the {camera_name} camera."}, trigger=trigger, evidence=event)
            actions.append("spoke")
        if away and siris.settings.intruder_auto_lockdown and service.get_protocol(siris, "lockdown") is not None:
            service.run(siris, "lockdown", trigger + " (SIRISAI_INTRUDER_AUTO_LOCKDOWN)")
            actions.append("lockdown")
        siris.events.log_event("intruder_alert", entity_name=camera, data={"event_id": event.get("id"), "message": message, "actions": actions})
        return {"camera": camera, "event_id": event.get("id"), "message": message, "actions": actions}
