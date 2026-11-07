"""People (SirisAI 5.0): birthdays, anniversaries and keeping in touch.

- **Who**: added by hand (POST /siris/people, SirisOS, or "remember Dom's
  birthday is 14 March"), and found in the calendar: a yearly event called
  "Dom's birthday" or "Birthday: Dom" adds Dom with that date.
- **Reminders**: SIRISAI_PEOPLE_REMIND_DAYS (7) before a birthday or
  anniversary, and on the day, the attention feed says so, with gift ideas
  from what Siris (the second brain) and your memories say about them
  ("likes hiking", "wants a pour-over set"), and how old they're turning
  when the year is known.
- **Keeping in touch**: "when did I last catch up with Dom?" looks at the
  calendar (events naming them or with them invited) and catch-ups you've
  logged ("I had lunch with Dom today"), and anyone with a keep-in-touch
  gap (keep_in_touch_days) you've gone past is mentioned once a week.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from app.core import clock
from app.core.user_context import get_current_user_id
from app.db.models import Person

if TYPE_CHECKING:
    from app.core.siris import SirisAI

FIELDS = ("name", "relation", "birthday", "anniversary", "notes", "last_contact", "keep_in_touch_days")
_BIRTHDAY = re.compile(r"^(?:(?P<a>.+?)['’]s? (?:birthday|bday)|(?:birthday|bday)[: -]+(?P<b>.+?))(?: 🎂)?$", re.I)
_GIFTY = re.compile(r"\b(likes?|loves?|wants?|into|enjoys?|wishlist|would love|collects?|favourite|favorite)\b", re.I)


def _today(siris: "SirisAI", now: datetime | None = None) -> date:
    return (now or datetime.now(UTC)).astimezone(clock.get_zone(siris.settings.timezone)).date()


def _date_field(value: Any) -> str | None:
    """"YYYY-MM-DD" or "MM-DD" (no year known)."""
    if value in (None, ""):
        return None
    text = str(value).strip()
    if re.fullmatch(r"\d{2}-\d{2}", text):
        date(2000, int(text[:2]), int(text[3:]))  # validates (29 Feb included)
        return text
    return date.fromisoformat(text[:10]).isoformat()


def _serialize(row: Person, today: date | None = None) -> dict[str, Any]:
    out = {k: getattr(row, k) for k in FIELDS}
    out["id"] = row.id
    if today:
        for key in ("birthday", "anniversary"):
            nxt = next_date(getattr(row, key), today)
            if nxt:
                out[f"next_{key}"] = nxt.isoformat()
                out[f"days_to_{key}"] = (nxt - today).days
        if row.birthday and len(row.birthday) == 10:
            nxt = next_date(row.birthday, today)
            out["turning"] = nxt.year - int(row.birthday[:4]) if nxt else None
    return out


def next_date(value: str | None, today: date) -> date | None:
    if not value:
        return None
    month, day = (int(value[-5:-3]), int(value[-2:]))
    for year in (today.year, today.year + 1):
        try:
            candidate = date(year, month, day)
        except ValueError:  # 29 February in a short year: the 28th
            candidate = date(year, month, 28)
        if candidate >= today:
            return candidate
    return None


def add(siris: "SirisAI", item: dict[str, Any]) -> dict[str, Any]:
    name = str(item.get("name") or "").strip()
    if not name:
        raise ValueError("A person needs a name.")
    existing = _by_name(siris, name)
    if existing is not None:
        return update(siris, existing.id, item)
    data = {k: item.get(k) for k in FIELDS if item.get(k) not in (None, "")}
    for key in ("birthday", "anniversary", "last_contact"):
        if key in data:
            data[key] = _date_field(data[key])
    with siris.conversation._session_factory() as session:
        row = Person(user_id=get_current_user_id(), **data)
        session.add(row)
        session.commit()
        return _serialize(row, _today(siris))


def _by_name(siris: "SirisAI", name: str) -> Person | None:
    with siris.conversation._session_factory() as session:
        rows = list(session.scalars(select(Person).where(Person.user_id == get_current_user_id())))
    wanted = name.lower().strip()
    exact = [r for r in rows if r.name.lower() == wanted]
    if exact:
        return exact[0]
    first = [r for r in rows if r.name.lower().split()[0] == wanted.split()[0]]
    return first[0] if len(first) == 1 else None


def update(siris: "SirisAI", person_id: int, changes: dict[str, Any]) -> dict[str, Any]:
    with siris.conversation._session_factory() as session:
        row = session.get(Person, person_id)
        if row is None or row.user_id != get_current_user_id():
            raise KeyError(person_id)
        for key in FIELDS:
            if key in changes and changes[key] not in (None, ""):
                value = changes[key]
                if key in ("birthday", "anniversary", "last_contact"):
                    value = _date_field(value)
                setattr(row, key, value)
        session.commit()
        return _serialize(row, _today(siris))


def delete(siris: "SirisAI", person_id: int) -> None:
    with siris.conversation._session_factory() as session:
        row = session.get(Person, person_id)
        if row is None or row.user_id != get_current_user_id():
            raise KeyError(person_id)
        session.delete(row)
        session.commit()


def everyone(siris: "SirisAI", now: datetime | None = None) -> list[dict[str, Any]]:
    today = _today(siris, now)
    with siris.conversation._session_factory() as session:
        rows = [_serialize(r, today) for r in session.scalars(select(Person).where(Person.user_id == get_current_user_id()))]
    return sorted(rows, key=lambda p: min(p.get("days_to_birthday", 999), p.get("days_to_anniversary", 999)))


def import_calendar(siris: "SirisAI", now: datetime | None = None) -> int:
    """People from yearly "X's birthday" events in the next year."""
    calendar = getattr(siris, "calendar", None)
    if calendar is None:
        return 0
    start = now or datetime.now(UTC)
    added = 0
    for event in calendar.events_between(start, start + timedelta(days=366)):
        m = _BIRTHDAY.match((event.get("summary") or "").strip())
        if not m or not event.get("start"):
            continue
        name = (m.group("a") or m.group("b") or "").strip()
        if not name or len(name) > 60:
            continue
        when = str(event["start"])[5:10]
        person = _by_name(siris, name)
        if person is None:
            add(siris, {"name": name, "birthday": when})
            added += 1
        elif not person.birthday:
            update(siris, person.id, {"birthday": when})
    return added


def gift_ideas(siris: "SirisAI", name: str, limit: int = 4) -> list[str]:
    """Things your notes and memories say they like or want."""
    ideas: list[str] = []
    first = name.split()[0]
    try:
        for m in siris.memory.recall(query=f"{first} likes wants gift", limit=10):
            text = m.get("content") or ""
            if first.lower() in text.lower() and _GIFTY.search(text):
                ideas.append(text[:140])
    except Exception:  # noqa: BLE001
        pass
    brain = getattr(siris, "second_brain", None)
    if brain is not None:
        try:
            for hit in brain.search(f"{first} likes", limit=5):
                for excerpt in hit.get("excerpts", [])[:2]:
                    if first.lower() in excerpt.lower() and _GIFTY.search(excerpt):
                        ideas.append(excerpt[:140])
        except Exception:  # noqa: BLE001
            pass
    return list(dict.fromkeys(ideas))[:limit]


def last_seen(siris: "SirisAI", name: str, now: datetime | None = None, days: int = 365) -> dict[str, Any]:
    """When you last saw or spoke to someone: the calendar and logged catch-ups."""
    now = now or datetime.now(UTC)
    person = _by_name(siris, name)
    first = (person.name if person else name).split()[0].lower()
    best: tuple[str, str] | None = None
    calendar = getattr(siris, "calendar", None)
    if calendar is not None:
        try:
            for event in calendar.events_between(now - timedelta(days=days), now):
                who = " ".join([event.get("summary") or "", *[str(a) for a in event.get("attendees") or []]]).lower()
                if re.search(rf"\b{re.escape(first)}\b", who) and not _BIRTHDAY.match(event.get("summary") or ""):
                    when = str(event.get("start"))[:10]
                    if best is None or when > best[0]:
                        best = (when, event.get("summary") or "")
        except Exception:  # noqa: BLE001
            pass
    if person and person.last_contact and (best is None or person.last_contact > best[0]):
        best = (person.last_contact, "logged catch-up")
    if best is None:
        return {"name": person.name if person else name, "last": None, "text": f"Nothing in the calendar with {name} in the last year."}
    ago = (now.date() - date.fromisoformat(best[0])).days
    return {"name": person.name if person else name, "last": best[0], "what": best[1], "days_ago": ago,
            "text": f"{ago} days ago ({best[0]}): {best[1]}."}


def caught_up(siris: "SirisAI", name: str, on: str | None = None) -> dict[str, Any]:
    on = _date_field(on) or _today(siris).isoformat()
    person = _by_name(siris, name)
    if person is None:
        return add(siris, {"name": name, "last_contact": on})
    return update(siris, person.id, {"last_contact": on})


def check(siris: "SirisAI", now: datetime | None = None) -> list[dict[str, Any]]:
    today = _today(siris, now)
    ahead = siris.settings.people_remind_days
    wanted = []
    for person in everyone(siris, now):
        for kind, label in (("birthday", "birthday"), ("anniversary", "anniversary")):
            days = person.get(f"days_to_{kind}")
            if days is None or days > ahead:
                continue
            when = "today" if days == 0 else "tomorrow" if days == 1 else f"in {days} days ({date.fromisoformat(person[f'next_{kind}']).strftime('%a %-d %b')})"
            turning = f", turning {person['turning']}" if kind == "birthday" and person.get("turning") else ""
            ideas = gift_ideas(siris, person["name"]) if days > 0 else []
            wanted.append({
                "key": f"people:{kind}:{person['id']}:{person[f'next_{kind}']}", "kind": "notice",
                "severity": "attention" if days <= 1 else "info",
                "title": f"{person['name']}'s {label} is {when}{turning}",
                "body": ("Gift ideas from your notes: " + "; ".join(ideas) + "." if ideas else
                         "Nothing in your notes about what they'd like: tell me and I'll remember for next time.") if days > 0 else "Say happy birthday!" if kind == "birthday" else "Happy anniversary!",
            })
    # Keep in touch: past the gap, mentioned on Sundays only.
    if today.weekday() == 6:
        for person in everyone(siris, now):
            gap = person.get("keep_in_touch_days")
            if not gap:
                continue
            seen = last_seen(siris, person["name"], now)
            if seen.get("days_ago") is None or seen["days_ago"] > gap:
                wanted.append({
                    "key": f"people:touch:{person['id']}:{today.isoformat()}", "kind": "suggestion", "severity": "info",
                    "title": f"It's been a while since you caught up with {person['name']}",
                    "body": seen["text"],
                })
    siris.attention.sync("people", wanted, outcome="Passed")
    return wanted
