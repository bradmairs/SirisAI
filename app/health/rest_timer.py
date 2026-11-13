"""A rest timer between sets (6.48): "start a 2-minute rest".

gym_rest_timer starts it (from chat, voice or the HUD; also a quick
command, so no model is needed). The scheduler's `rest_timer` job, every 5
seconds, says "Rest's up" when it ends. When the Watch's heart rate is
arriving live (the Health Metrics automation sending each reading), it
says so sooner once your heart rate is back below
SIRISAI_REST_RECOVERED_BPM (110): "Heart rate's down to 104, go when
ready". One timer at a time; starting another replaces it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.core.siris import SirisAI

STATE_KEY = "gym:rest_timer"
MIN_SECONDS, MAX_SECONDS = 15, 900
FRESH_SECONDS = 20  # a heart-rate reading older than this isn't "now"


def start(siris: "SirisAI", seconds: int = 120, exercise: str | None = None, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    seconds = max(MIN_SECONDS, min(int(seconds or 120), MAX_SECONDS))
    timer = {"started": now.isoformat(), "ends": (now + timedelta(seconds=seconds)).isoformat(), "seconds": seconds, "exercise": exercise}
    siris.state.set(STATE_KEY, timer)
    words = f"{seconds // 60} minute{'s' if seconds // 60 != 1 else ''}" if seconds % 60 == 0 else f"{seconds} seconds"
    return {**timer, "message": f"Resting {words}. I'll tell you when it's up."}


def cancel(siris: "SirisAI") -> dict[str, Any]:
    had = siris.state.get(STATE_KEY)
    siris.state.delete(STATE_KEY)
    return {"cancelled": bool(had), "message": "Rest timer stopped." if had else "There's no rest timer running."}


def _say(siris: "SirisAI", message: str) -> None:
    if siris.tools.get("notify") is not None:
        try:
            siris.run_tool("notify", {"message": message, "title": "Rest", "priority": "high"}, trigger="rest timer you started")
            return
        except Exception:  # noqa: BLE001 - fall back to the feed
            pass
    siris.attention.raise_item("gym:rest", "alert", message, source="gym-rest", severity="info")


def check(siris: "SirisAI", now: datetime | None = None) -> str | None:
    """Say it's time when it is; returns what was said."""
    timer = siris.state.get(STATE_KEY)
    if not timer:
        return None
    now = now or datetime.now(UTC)
    started, ends = datetime.fromisoformat(timer["started"]), datetime.fromisoformat(timer["ends"])
    message = None
    if now >= ends:
        message = "Rest's up" + (f": next set of {timer['exercise']}." if timer.get("exercise") else ".")
    elif (now - started).total_seconds() >= 30:
        from app.health import gym_effort

        recent = gym_effort.samples_between(siris, now - timedelta(seconds=FRESH_SECONDS), now)
        if recent:
            bpm = float(recent[-1][1])
            if bpm <= siris.settings.rest_recovered_bpm:
                left = int((ends - now).total_seconds())
                message = f"Heart rate's down to {bpm:.0f}, go when ready ({left} s of rest left)."
    if message:
        siris.state.delete(STATE_KEY)
        _say(siris, message)
    return message
