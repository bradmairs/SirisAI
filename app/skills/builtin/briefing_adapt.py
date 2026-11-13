"""The morning brief, adapted (6.58).

- **What changed first**: each part of the brief is compared with
  yesterday's. Parts that changed come first; parts word-for-word the same
  as yesterday (the same to-do list, the same goals, "all systems normal")
  are folded into one line at the end: "Same as yesterday: to-do, goals".
- **Your order and what to leave out**: briefing_preferences hides parts
  you don't want ("leave the Plex bit out of my briefing") and puts others
  first ("weather first"). Kept in state `briefing:prefs`.

Only folds against the brief from exactly the day before, so a second brief
the same day is never shortened.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.core.siris import SirisAI

PREFS_KEY = "briefing:prefs"
LAST_KEY = "briefing:last_parts"
SECTIONS = {
    "weather": r"^Weather:", "forecast": r"^Forecast:", "calendar": r"^(Today:|Nothing on the calendar)", "todo": r"^To-do",
    "reminders": r"^Reminders today:", "plex": r"^(Recently watched|Nothing recently watched)", "warnings": r"^(\d+ warning|All systems normal)",
    "queued": r"^Also since last time:", "goals": r"^(Active goals|No active goals)", "health": r"^(Readiness|Health|Sleep)",
    "training": r"^(Training last week|Readiness is \d+ \(recover\))", "email": r"^(Email|\d+ (?:new|unread) email)", "car": r"^(Car|The car)",
    "parcels": r"^(Parcel|\d+ parcel)", "birthdays": r"birthday", "on_this_day": r"^On this day",
}
NAMES = {"todo": "to-do", "plex": "Plex", "warnings": "systems", "queued": "earlier notifications", "on_this_day": "on this day"}


def section(line: str) -> str:
    for name, pattern in SECTIONS.items():
        if re.search(pattern, line, re.I):
            return name
    return "other:" + " ".join(line.split()[:2]).lower()


def split(summary: str) -> list[str]:
    """The brief's sentences, one per part (parts end with '.' then a capital)."""
    return [p.strip() for p in re.split(r"(?<=[.!?])\s+(?=[A-Z0-9])", summary) if p.strip()]


def prefs(siris: "SirisAI") -> dict[str, list[str]]:
    p = siris.state.get(PREFS_KEY) or {}
    return {"hidden": list(p.get("hidden") or []), "first": list(p.get("first") or [])}


def set_prefs(siris: "SirisAI", hide: list[str] | None = None, show: list[str] | None = None, first: list[str] | None = None) -> dict[str, Any]:
    p = prefs(siris)
    known = set(SECTIONS)

    def norm(items: list[str] | None) -> list[str]:
        out = []
        for x in items or []:
            key = {"to-do": "todo", "todos": "todo", "systems": "warnings", "goal": "goals", "reminder": "reminders"}.get(x.strip().lower(), x.strip().lower())
            if key not in known:
                raise ValueError(f"'{x}' isn't a part of the brief. Parts: {', '.join(sorted(known))}.")
            out.append(key)
        return out

    hidden = [h for h in p["hidden"] if h not in norm(show)] + [h for h in norm(hide) if h not in p["hidden"]]
    order = norm(first) or p["first"]
    siris.state.set(PREFS_KEY, {"hidden": hidden, "first": order})
    return {"hidden": hidden, "first": order, "message": "Briefing: " + (f"leaving out {', '.join(hidden)}" if hidden else "nothing left out")
            + (f"; {', '.join(order)} first" if order else "") + "."}


def adapt(siris: "SirisAI", summary: str, today: date) -> str:
    parts = [(section(line), line) for line in split(summary)]
    p = prefs(siris)
    parts = [(k, line) for k, line in parts if k not in p["hidden"]]
    last = siris.state.get(LAST_KEY) or {}
    siris.state.set(LAST_KEY, {"date": today.isoformat(), "parts": {k: line for k, line in parts}})
    same: list[str] = []
    if last.get("date") == (today - timedelta(days=1)).isoformat():
        before = last.get("parts") or {}
        kept = []
        for k, line in parts:
            if before.get(k) == line and k not in ("weather", "forecast", "calendar", "reminders"):
                same.append(NAMES.get(k, k.split(":", 1)[-1]))
            else:
                kept.append((k, line))
        parts = kept
    rank = {k: i for i, k in enumerate(p["first"])}
    parts = sorted(parts, key=lambda kl: rank.get(kl[0], len(rank)))  # stable: the rest keep their order
    text = " ".join(line for _, line in parts)
    if same:
        text += f" Same as yesterday: {', '.join(same)}."
    return text
