"""Where a notification goes (5.8): spoken at home, pushed when out, held overnight.

Every push passes through here (it wraps the ntfy client), so the rules are the same for the
notify tool, reminders and background jobs alike:

- **High or urgent** (reminders are high): pushed straight away, whatever the time.
- **Quiet hours** (SIRISAI_QUIET_HOURS, default 22-7): anything else is held and read out in the
  morning briefing ("Also since last time").
- **At home with the HUD open** (presence says home, and a HUD that can play sound checked in
  within the last two minutes): spoken on the HUD instead of buzzing the phone. If the HUD doesn't
  confirm it played within SPOKEN_TIMEOUT seconds, it's pushed to the phone after all.
- Otherwise pushed, as before. Pushes with action buttons always go to the phone.
"""

from __future__ import annotations

import itertools
import logging
import threading
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from app.core import clock
from app.notify.client import NotifyClient, _ntfy_priority

if TYPE_CHECKING:
    from app.core.siris import SirisAI

logger = logging.getLogger(__name__)

HUD_SEEN_KEY = "hud_speaker_seen"     # time.time() a HUD able to play sound last checked in
HUD_FRESH_SECONDS = 120
SPOKEN_TIMEOUT = 90.0
_ids = itertools.count(1)


def in_quiet_hours(siris: "SirisAI", now: datetime | None = None) -> bool:
    spec = (getattr(siris.settings, "quiet_hours", "") or "").strip()
    if not spec:
        return False
    try:
        start, end = (int(x) for x in spec.split("-"))
    except ValueError:
        return False
    hour = (now or datetime.now(UTC)).astimezone(clock.get_zone(siris.settings.timezone)).hour
    return start <= hour < end if start < end else hour >= start or hour < end


def is_home(siris: "SirisAI") -> bool:
    from app.skills.builtin.presence import _PRESENCE_ENTITY_NAME

    try:
        last = siris.events.recent_events(entity_name=_PRESENCE_ENTITY_NAME, limit=1)
    except Exception:  # noqa: BLE001
        return False
    return bool(last and (last[0].get("data") or {}).get("home"))


def hud_listening(siris: "SirisAI") -> bool:
    return time.time() - float(siris.state.get(HUD_SEEN_KEY) or 0) < HUD_FRESH_SECONDS


def note_hud_seen(siris: "SirisAI") -> None:
    siris.state.set(HUD_SEEN_KEY, time.time())


class RoutedNotifyClient:
    """Drop-in for NotifyClient (same send/close) that decides where each message goes."""

    def __init__(self, inner: NotifyClient, siris: "SirisAI") -> None:
        self.inner = inner
        self._siris = siris
        self._lock = threading.Lock()
        self._announcements: dict[int, dict[str, Any]] = {}

    # ---- the client interface --------------------------------------------------------------

    def send(self, message: str, title: str | None = None, actions: list[dict[str, Any]] | None = None,
             priority: str | None = None, now: datetime | None = None) -> str:
        """Returns where it went: "pushed", "held" or "spoken"."""
        level = _ntfy_priority(priority) or 3
        siris = self._siris
        if level < 4 and not actions:
            if in_quiet_hours(siris, now):
                siris.pending_notifications.queue(f"{title}: {message}" if title else message, "notify", "quiet hours")
                return "held"
            if getattr(siris.settings, "notify_speak_at_home", True) and is_home(siris) and hud_listening(siris):
                self._announce(message, title, priority)
                return "spoken"
        self.inner.send(message, title=title, actions=actions, priority=priority)
        return "pushed"

    def close(self) -> None:
        self.inner.close()

    # ---- the HUD's side --------------------------------------------------------------------

    def _announce(self, message: str, title: str | None, priority: str | None) -> None:
        aid = next(_ids)
        with self._lock:
            self._announcements[aid] = {"id": aid, "title": title or "", "message": message, "at": datetime.now(UTC).isoformat()}
        timer = threading.Timer(SPOKEN_TIMEOUT, self._fall_back, args=(aid, priority))
        timer.daemon = True
        timer.start()

    def _fall_back(self, aid: int, priority: str | None) -> None:
        with self._lock:
            item = self._announcements.pop(aid, None)
        if item is None:
            return   # heard
        try:
            self.inner.send(item["message"], title=item["title"] or None, priority=priority)
        except Exception:  # noqa: BLE001
            logger.warning("Pushing an unheard announcement failed", exc_info=True)

    def pending(self) -> list[dict[str, Any]]:
        with self._lock:
            return sorted(self._announcements.values(), key=lambda a: a["id"])

    def heard(self, aid: int) -> bool:
        with self._lock:
            return self._announcements.pop(aid, None) is not None
