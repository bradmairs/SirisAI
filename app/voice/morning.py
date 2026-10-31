"""The morning brief, spoken (4.03): the first time someone walks into the
kitchen in the morning, a short brief is read out on a speaker through Home
Assistant.

- **When**: SIRISAI_SPOKEN_BRIEF_TRIGGER (a motion or presence sensor, e.g.
  binary_sensor.kitchen_motion) turns on within SIRISAI_SPOKEN_BRIEF_HOURS
  (local, "5-11"), once a day. Checked with the scheduler's tick (20 s).
- **Where**: SIRISAI_SPOKEN_BRIEF_PLAYER (a media_player), through
  SIRISAI_SPOKEN_BRIEF_TTS (a Home Assistant TTS entity such as tts.piper),
  with HA's tts.speak.
- **What**: the weather, today's first events, readiness, energy and strain
  (when there's health data), and how many things need you. Never in guest
  mode, since it says things about your day out loud.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from app.core import clock, guest

if TYPE_CHECKING:
    from app.core.siris import SirisAI

logger = logging.getLogger(__name__)
DONE_KEY = "spoken_brief_day"
FRESH = timedelta(minutes=3)


def configured(siris: "SirisAI") -> bool:
    s = siris.settings
    return bool(s.spoken_brief_trigger and s.spoken_brief_player and s.spoken_brief_tts) and getattr(siris, "_home_assistant_client", None) is not None


def window(siris: "SirisAI") -> tuple[int, int]:
    try:
        a, b = (int(x) for x in siris.settings.spoken_brief_hours.split("-", 1))
        return a, b
    except ValueError:
        return 5, 11


def _clock(start: Any) -> str:
    text = str(start or "")
    if len(text) <= 10:
        return "all day"
    try:
        t = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return ""
    h = t.hour % 12 or 12
    return f"{h}{'' if t.minute == 0 else f':{t.minute:02d}'} {'am' if t.hour < 12 else 'pm'}"


def text(siris: "SirisAI") -> str:
    from app.health import overview
    from app.hub import compose

    b = compose.brief(siris)
    parts = ["Good morning."]
    w = b.weather
    if w and w.today:
        t = w.today
        bits = [f"{t.conditions.lower()}" if t.conditions else "", f"up to {round(t.high_c)} degrees" if t.high_c is not None else ""]
        line = "Today: " + ", ".join(x for x in bits if x)
        if t.rain_chance_percent and t.rain_chance_percent >= 40:
            line += f", with a {round(t.rain_chance_percent)} percent chance of rain"
        parts.append(line + ".")
    events = [e for e in (b.calendar or []) if not e.all_day][:3]
    if events:
        parts.append("On today: " + "; ".join(f"{e.title} at {_clock(e.start)}" for e in events) + ".")
    elif b.calendar is not None:
        parts.append("Nothing in the calendar.")
    try:
        h = overview.build(siris)
        r, e, st = h.get("readiness"), h.get("energy"), h.get("strain")
        if r:
            parts.append(f"Readiness is {r['score']}, {r['band'].lower()}. {r['advice']}")
        if st:
            parts.append(f"Aim for a strain of {st['target'][0]} to {st['target'][1]}.")
        if e and not r:
            parts.append(f"Energy is at {e['now']}.")
    except Exception:  # noqa: BLE001 - the brief speaks without health
        logger.info("Spoken brief: health unavailable", exc_info=True)
    a = b.attention
    if a.urgent:
        parts.append(f"{a.urgent} urgent thing{'s' if a.urgent != 1 else ''} in your inbox.")
    elif a.open:
        parts.append(f"{a.open} thing{'s' if a.open != 1 else ''} in your inbox when you're ready.")
    return " ".join(parts)


def check(siris: "SirisAI", now: datetime | None = None) -> dict[str, Any] | None:
    """Speak it if it's time; returns what was said, else None."""
    if not configured(siris) or guest.is_on(siris):
        return None
    local = (now or clock.now_local(siris.settings.timezone))
    start, end = window(siris)
    today = local.date().isoformat()
    if not (start <= local.hour < end) or siris.state.get(DONE_KEY) == today:
        return None
    ha = siris._home_assistant_client  # type: ignore[attr-defined]
    state = ha.get_state_or_none(siris.settings.spoken_brief_trigger)
    if not state or state.get("state") != "on":
        return None
    changed = state.get("last_changed")
    if changed:
        try:
            when = datetime.fromisoformat(str(changed).replace("Z", "+00:00"))
            if local.astimezone(when.tzinfo) - when > FRESH:
                return None  # on since before the window opened, or a sensor stuck on
        except ValueError:
            pass
    said = text(siris)
    siris.state.set(DONE_KEY, today)
    ha.call_service("tts", "speak", entity_id=siris.settings.spoken_brief_tts,
                    media_player_entity_id=siris.settings.spoken_brief_player, message=said)
    siris.audit.record("spoken_brief", {"player": siris.settings.spoken_brief_player}, result={"chars": len(said)}, trigger=f"{siris.settings.spoken_brief_trigger} turned on")
    return {"spoken": said}
