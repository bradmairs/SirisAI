"""A brief that's ready before you ask (4.31.0).

"Brief me" was the request said most to SirisAI (4 of 7 spoken requests,
13-22 Nov) and the slowest: 12-19 s before the first word, because the
daily_briefing skill gathered everything (6-11 s) and then the model
summarised it. Now the short spoken brief (app/voice/morning.py: weather,
today's events, readiness and strain, news, the inbox) is rebuilt every
10 minutes in waking hours, and "brief me" is a quick command
(app/cortex/quick.py) that reads it out with no model call: under a second.
Asking for more ("give me the full briefing", "brief me for tomorrow")
still goes to the model and the full skill.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from app.core import clock

if TYPE_CHECKING:
    from app.core.siris import SirisAI

logger = logging.getLogger(__name__)

KEY = "brief_ready"           # {"text", "at" (epoch), "hour"}
FRESH_SECONDS = 20 * 60
REFRESH_SECONDS = 10 * 60
WAKING_HOURS = (5, 23)        # local; outside these it's built when asked


def greeting(hour: int) -> str:
    return "Good morning." if hour < 12 else "Good afternoon." if hour < 18 else "Good evening."


def build(siris: "SirisAI") -> str:
    from app.voice import morning

    hour = clock.now_local(siris.settings.timezone).hour
    text = morning.text(siris)
    return greeting(hour) + text.removeprefix("Good morning.")


def refresh(siris: "SirisAI", force: bool = False) -> dict[str, Any] | None:
    """Rebuild the ready brief (the scheduler's `brief_ready` job)."""
    hour = clock.now_local(siris.settings.timezone).hour
    if not force and not (WAKING_HOURS[0] <= hour < WAKING_HOURS[1]):
        return None
    ready = {"text": build(siris), "at": time.time(), "hour": hour}
    siris.state.set(KEY, ready)
    return ready


def get(siris: "SirisAI") -> str:
    """The ready brief if it's fresh and from this part of the day, else one built now."""
    ready = siris.state.get(KEY) or {}
    hour = clock.now_local(siris.settings.timezone).hour
    if ready.get("text") and time.time() - float(ready.get("at") or 0) < FRESH_SECONDS \
            and greeting(int(ready.get("hour", -1))) == greeting(hour):
        return str(ready["text"])
    return str((refresh(siris, force=True) or {}).get("text") or build(siris))
