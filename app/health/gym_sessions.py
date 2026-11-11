"""Asking about SirisGym workouts (5.6): "what did I do at the gym on Monday",
"how many sets of squats last week", "show me my last leg day", "what
weight did I bench on the 3rd".

SirisOS holds every workout. SirisAI reads them with its service key
(GET /api/v1/gym/sessions?month= and /sessions/{id}, SirisOS 8.5 or later);
when SirisOS can't be reached it falls back to the workouts SirisGym sent
here for the heart-rate analysis (only those with set times). Each workout
comes back with every exercise and set (weight × reps, warm-ups and drop
sets marked, RPE), its records, notes and, when it lines up with a Watch
workout, the effort analysis from gym_effort.

Gym data is health data: the tool is in PRIVATE_TOOLS, so with
SIRISAI_HEALTH_PRIVATE on it only goes to the local model.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any

from app.core import clock

if TYPE_CHECKING:
    from app.core.siris import SirisAI

logger = logging.getLogger(__name__)
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
MAX_DETAIL = 10
_ALIASES = {"bench": "bench press", "ohp": "overhead press", "rdl": "romanian deadlift", "dl": "deadlift"}


def _today(siris: "SirisAI") -> date:
    return clock.now_local(siris.settings.timezone).date()


def resolve_day(siris: "SirisAI", text: str | None) -> date | None:
    """'2026-11-09', 'today', 'yesterday', or a weekday (the most recent one, today included)."""
    if not text:
        return None
    t = text.strip().lower()
    today = _today(siris)
    if t == "today":
        return today
    if t == "yesterday":
        return today - timedelta(days=1)
    for i, name in enumerate(WEEKDAYS):
        if t in (name, name[:3], f"last {name}", f"on {name}"):
            back = (today.weekday() - i) % 7
            if t.startswith("last") and back == 0:
                back = 7
            return today - timedelta(days=back)
    try:
        return date.fromisoformat(t[:10])
    except ValueError:
        raise ValueError(f"Give the day as YYYY-MM-DD, today, yesterday or a weekday, not {text!r}.") from None


def _months(a: date, b: date) -> list[str]:
    out, y, m = [], a.year, a.month
    while (y, m) <= (b.year, b.month):
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def _matches(name: str, exercise: str) -> bool:
    want = _ALIASES.get(exercise.strip().lower(), exercise.strip().lower())
    words = [w.rstrip("s") for w in re.split(r"\W+", want) if w]
    have = name.lower()
    return all(w in have for w in words)


# -- reading: SirisOS first, what SirisGym sent here otherwise ----------------------------


def _client(siris: "SirisAI") -> Any:
    return getattr(siris, "_sirisos_client", None)


def _from_timeline(t: dict[str, Any]) -> dict[str, Any]:
    """A workout as SirisGym sends it for the heart-rate analysis, in the same shape as SirisOS's."""
    entries: list[dict[str, Any]] = []
    for s in t.get("sets") or []:
        if not entries or entries[-1]["name"] != s.get("name"):
            entries.append({"exercise": s.get("exercise"), "name": s.get("name") or s.get("exercise"), "sets": [], "note": ""})
        entries[-1]["sets"].append({k: s.get(k) for k in ("weight", "reps", "seconds", "type", "rpe")} | {"done": True})
    return {"id": str(t.get("id")), "title": t.get("title") or "Workout", "started_at": t.get("started_at"), "ended_at": t.get("ended_at"),
            "entries": entries, "note": "", "records": []}


def _local(siris: "SirisAI") -> list[dict[str, Any]]:
    from app.health import gym_effort

    kept = siris.state.get(gym_effort._key("gym_sessions")) or {}
    return sorted((_from_timeline(t) for t in kept.values()), key=lambda s: str(s.get("started_at")), reverse=True)


def _day(s: dict[str, Any]) -> str:
    return str(s.get("date") or s.get("started_at") or "")[:10]


def find(siris: "SirisAI", since: date, until: date) -> tuple[list[dict[str, Any]], str]:
    """Workouts between two days (newest first), and where they came from."""
    client = _client(siris)
    if client is not None:
        try:
            found: list[dict[str, Any]] = []
            for month in _months(since, until):
                found.extend(client.gym_sessions(month))
            picked = [s for s in found if since.isoformat() <= _day(s) <= until.isoformat()]
            return sorted(picked, key=lambda s: str(s.get("started_at")), reverse=True), "sirisos"
        except Exception as exc:  # noqa: BLE001 - fall back to what's kept here
            logger.info("Couldn't read SirisGym's workouts from SirisOS: %s", exc)
    picked = [s for s in _local(siris) if since.isoformat() <= _day(s) <= until.isoformat()]
    return picked, "kept here"


def detail(siris: "SirisAI", session: dict[str, Any], source: str) -> dict[str, Any]:
    if source == "sirisos" and "entries" not in session:
        try:
            return _client(siris).gym_session(str(session["id"]))
        except Exception as exc:  # noqa: BLE001
            logger.info("Couldn't read workout %s from SirisOS: %s", session.get("id"), exc)
            local = next((s for s in _local(siris) if s["id"] == str(session["id"])), None)
            return local or session
    return session


# -- in words ---------------------------------------------------------------------------


def _num(x: Any) -> str:
    return f"{float(x):g}"


def set_text(s: dict[str, Any]) -> str:
    if s.get("weight") and s.get("reps"):
        text = f"{_num(s['weight'])} kg × {s['reps']}"
    elif s.get("reps"):
        text = f"{s['reps']} reps"
    elif s.get("seconds"):
        text = f"{s['seconds']}s" if s["seconds"] < 120 else f"{round(s['seconds'] / 60)} min"
        if s.get("km"):
            text += f", {_num(s['km'])} km"
    elif s.get("km"):
        text = f"{_num(s['km'])} km"
    else:
        text = "a set"
    if s.get("rpe"):
        text += f" @{_num(s['rpe'])}"
    kind = s.get("type")
    if kind and kind != "normal":
        text += {"warmup": " (warm-up)", "drop": " (drop set)", "failure": " (to failure)"}.get(kind, "")
    return text


def _e1rm(s: dict[str, Any]) -> float | None:
    from app.health.gym_effort import _e1rm

    return None if s.get("type") == "warmup" else _e1rm(s.get("weight"), s.get("reps"))


def _minutes(s: dict[str, Any]) -> int | None:
    if (s.get("summary") or {}).get("minutes") is not None:
        return s["summary"]["minutes"]
    if s.get("minutes") is not None:
        return s["minutes"]
    try:
        a, b = datetime.fromisoformat(str(s["started_at"])), datetime.fromisoformat(str(s["ended_at"]))
    except (KeyError, TypeError, ValueError):
        return None
    return round((b - a).total_seconds() / 60)


def _when(s: dict[str, Any]) -> str:
    try:
        return date.fromisoformat(_day(s)).strftime("%a %-d %b")
    except ValueError:
        return _day(s)


def describe(siris: "SirisAI", s: dict[str, Any], exercise: str | None = None) -> dict[str, Any]:
    from app.health import gym_effort

    entries = []
    for e in s.get("entries") or []:
        name = e.get("name") or e.get("exercise") or "?"
        if exercise and not _matches(name, exercise):
            continue
        sets = [x for x in e.get("sets") or [] if x.get("done", True)]
        if not sets:
            continue
        best = max((v for v in (_e1rm(x) for x in sets) if v), default=None)
        entries.append({
            "name": name, "sets": [set_text(x) for x in sets], "working_sets": sum(1 for x in sets if x.get("type") != "warmup"),
            "best_e1rm": best, "note": e.get("note") or "",
        })
    minutes = _minutes(s)
    working = sum(e["working_sets"] for e in entries)
    head = f"{_when(s)}, {s.get('title') or 'Workout'}" + (f" ({minutes} min, {working} sets)" if minutes else f" ({working} sets)")
    lines = [f"{e['name']}: " + ", ".join(e["sets"]) + (f" (note: {e['note']})" if e["note"] else "") for e in entries]
    records = [r.get("text") for r in s.get("records") or [] if r.get("text") and (not exercise or _matches(str(r.get("name") or ""), exercise))]
    watch = None
    try:
        analysis = gym_effort.analyse(siris, str(s.get("id")))
        if analysis.get("linked") and analysis.get("text"):
            watch = analysis["text"]
    except Exception:  # noqa: BLE001 - the Watch part is extra
        logger.debug("No effort analysis for %s", s.get("id"), exc_info=True)
    text = head + ". " + "; ".join(lines) + "."
    if records:
        text += " Records: " + "; ".join(records) + "."
    if s.get("note"):
        text += f" Note: {s['note']}."
    if watch and not exercise:
        text += f" Watch: {watch}"
    return {"id": s.get("id"), "date": _day(s), "title": s.get("title"), "minutes": minutes, "exercises": entries,
            "records": records, "note": s.get("note") or "", "watch": watch, "text": text}


def _summary_line(s: dict[str, Any]) -> str:
    minutes = _minutes(s)
    bits = [f"{minutes} min" if minutes else "", f"{s['sets']} sets" if s.get("sets") is not None else "",
            f"{_num(s['volume'])} kg lifted" if s.get("volume") else ""]
    names = s.get("names") or [e.get("name") for e in s.get("entries") or []]
    return f"{_when(s)}, {s.get('title') or 'Workout'} ({', '.join(b for b in bits if b)})" + (f": {', '.join(n for n in names if n)}" if names else "")


def ask(siris: "SirisAI", day: str | None = None, since: str | None = None, until: str | None = None,
        exercise: str | None = None, session_id: str | None = None, limit: int = 5) -> dict[str, Any]:
    limit = max(1, min(int(limit or 5), MAX_DETAIL))
    today = _today(siris)
    if session_id:
        client = _client(siris)
        session = None
        if client is not None:
            try:
                session, source = client.gym_session(session_id), "sirisos"
            except Exception as exc:  # noqa: BLE001
                logger.info("Couldn't read workout %s from SirisOS: %s", session_id, exc)
        if session is None:
            session, source = next((s for s in _local(siris) if s["id"] == session_id), None), "kept here"
        if session is None:
            return {"text": f"There's no SirisGym workout {session_id}.", "sessions": []}
        one = describe(siris, session, exercise)
        return {"text": one["text"], "sessions": [one], "source": source}

    on = resolve_day(siris, day)
    if on:
        a = b = on
    else:
        b = resolve_day(siris, until) or today
        a = resolve_day(siris, since) or b - timedelta(days=60 if exercise else 30)
        if a > b:
            a, b = b, a
    found, source = find(siris, a, b)
    if not found:
        if _client(siris) is None and not _local(siris):
            return {"text": "No SirisGym workouts here yet: SirisAI reads them from SirisOS (set SIRISAI_SIRISOS_URL and its service key) "
                            "or keeps the ones SirisGym sends with set times.", "sessions": []}
        span = _when({"date": a.isoformat()}) if a == b else f"{_when({'date': a.isoformat()})} to {_when({'date': b.isoformat()})}"
        return {"text": f"No gym workouts on {span}." if a == b else f"No gym workouts from {span}.", "sessions": [], "source": source}

    # One day, one lift, or a few workouts: in full. Otherwise a list, with the newest in full.
    full = on is not None or exercise is not None or len(found) <= 2
    shown: list[dict[str, Any]] = []
    for s in found:
        if len(shown) >= (limit if full else 1):
            break
        one = describe(siris, detail(siris, s, source), exercise)
        if exercise and not one["exercises"]:
            continue
        shown.append(one)
    if exercise and not shown:
        return {"text": f"No {exercise} in the gym workouts from {_when({'date': a.isoformat()})} to {_when({'date': b.isoformat()})}.",
                "sessions": [], "source": source}
    if full:
        text = " ".join(s["text"] for s in shown)
        if exercise:
            best = max((e["best_e1rm"] for s in shown for e in s["exercises"] if e["best_e1rm"]), default=None)
            if best:
                text += f" Best estimated one-rep max in these: {best:g} kg."
        more = len(found) - len(shown)
        if more > 0 and not exercise:
            text += f" ({more} more not shown.)"
    else:
        listed = found[: max(limit, 5) * 2]
        text = (f"{len(found)} gym workouts from {_when({'date': a.isoformat()})} to {_when({'date': b.isoformat()})}: "
                + "; ".join(_summary_line(s) for s in listed) + (f"; and {len(found) - len(listed)} more" if len(found) > len(listed) else "")
                + f". The latest in full: {shown[0]['text']}")
    return {"text": text, "sessions": shown, "count": len(found), "from": a.isoformat(), "to": b.isoformat(), "source": source}
