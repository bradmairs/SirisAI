"""Builds the hub contract's responses (app/hub/contract.py) from SirisAI's
tools and stores. Every section is best-effort on its own: a section whose
integration isn't configured is None, one that's configured but failed is
None and named in `unavailable`. In guest mode the personal sections are
left out entirely and named in `hidden`."""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time
from zoneinfo import ZoneInfo
from typing import TYPE_CHECKING, Any, Callable

from app.core import clock, guest
from app.core.user_context import get_current_user_id, set_current_user_id
from app.hub import contract as c

if TYPE_CHECKING:
    from app.core.siris import SirisAI

logger = logging.getLogger(__name__)

PERSONAL_SECTIONS = ["calendar", "todo", "email", "health", "car", "parcels", "brain"]


class _Missing(Exception):
    """The section's integration isn't configured: None, not 'unavailable'."""


def _tool(siris: "SirisAI", name: str, **args: Any) -> Any:
    found = siris.tools.get(name)
    if found is None:
        raise _Missing(name)
    return found.run(**args)


def _sections(siris: "SirisAI", builders: dict[str, Callable[[], Any]], hidden: list[str]) -> tuple[dict[str, Any], list[str]]:
    """Run every builder concurrently (each may do network I/O) with the
    request's user carried into the worker threads."""
    out: dict[str, Any] = {}
    unavailable: list[str] = []
    user = get_current_user_id()

    def run(name: str, fn: Callable[[], Any]) -> tuple[str, Any, bool]:
        set_current_user_id(user)
        try:
            return name, fn(), False
        except _Missing:
            return name, None, False
        except Exception:
            logger.info("hub section %s failed", name, exc_info=True)
            return name, None, True

    wanted = {k: v for k, v in builders.items() if k not in hidden}
    with ThreadPoolExecutor(max_workers=max(1, min(8, len(wanted)))) as pool:
        for name, value, failed in pool.map(lambda kv: run(*kv), wanted.items()):
            out[name] = value
            if failed:
                unavailable.append(name)
    for name in builders:
        out.setdefault(name, None)
    return out, sorted(unavailable)


def _guest(siris: "SirisAI") -> tuple[bool, list[str]]:
    on = guest.is_on(siris)
    return on, (list(PERSONAL_SECTIONS) if on else [])


# -- sections -------------------------------------------------------------------


def weather(siris: "SirisAI") -> c.Weather:
    now = today = None
    if siris.tools.get("weather_current") is None and siris.tools.get("weather_forecast") is None:
        raise _Missing("weather")
    current = _tool(siris, "weather_current") if siris.tools.get("weather_current") else None
    if isinstance(current, dict):
        now = c.WeatherNow(
            temperature_c=current.get("temperature_c"),
            conditions=current.get("conditions"),
            humidity_percent=current.get("humidity_percent"),
            wind_kmh=current.get("wind_speed_kmh", current.get("wind_kmh")),
        )
    forecast = _tool(siris, "weather_forecast", days=1) if siris.tools.get("weather_forecast") else None
    days = forecast.get("days") if isinstance(forecast, dict) else forecast
    if isinstance(days, list) and days and isinstance(days[0], dict):
        d = days[0]
        today = c.ForecastDay(
            date=d.get("date"),
            conditions=d.get("conditions"),
            high_c=d.get("high_c"),
            low_c=d.get("low_c"),
            rain_chance_percent=d.get("rain_chance_percent", d.get("precipitation_probability_percent")),
            precipitation_mm=d.get("rain_mm", d.get("precipitation_mm")),
        )
    return c.Weather(now=now, today=today)


def _day(d: dict[str, Any]) -> c.ForecastDay:
    return c.ForecastDay(
        date=d.get("date"),
        conditions=d.get("conditions"),
        high_c=d.get("high_c"),
        low_c=d.get("low_c"),
        rain_chance_percent=d.get("rain_chance_percent", d.get("precipitation_probability_percent")),
        precipitation_mm=d.get("rain_mm", d.get("precipitation_mm")),
    )


def forecast(siris: "SirisAI", days: int = 7) -> c.Forecast:
    """Up to a week of daily forecasts (not personal, so guest mode keeps it)."""
    if siris.tools.get("weather_forecast") is None:
        return c.Forecast(days=None, unavailable=True)
    try:
        raw = siris.tools.get("weather_forecast").run(days=max(1, min(7, days)))  # type: ignore[union-attr]
    except Exception as exc:  # noqa: BLE001 - reported as unavailable, never a 500
        logger.warning("Forecast unavailable: %s", exc)
        return c.Forecast(days=None, unavailable=True)
    raw = raw.get("days") if isinstance(raw, dict) else raw
    return c.Forecast(days=[_day(d) for d in raw if isinstance(d, dict)] if isinstance(raw, list) else None)


def _events(events: Any) -> list[c.CalendarEvent]:
    out = []
    for e in events if isinstance(events, list) else []:
        if not isinstance(e, dict):
            continue
        start = e.get("start")
        if isinstance(start, dict):
            start = start.get("dateTime") or start.get("date")
        all_day = bool(e.get("all_day")) or (isinstance(start, str) and len(start) <= 10)
        out.append(
            c.CalendarEvent(
                title=str(e.get("summary") or e.get("title") or "Event"),
                start=str(start) if start else None,
                end=str(e["end"]) if isinstance(e.get("end"), str) else None,
                all_day=all_day,
                location=e.get("location") or None,
                calendar=e.get("calendar") or None,
                attendees=[str(a) for a in e.get("attendees") or [] if isinstance(a, str)],
            )
        )
    return out


def calendar(siris: "SirisAI") -> list[c.CalendarEvent]:
    if siris.tools.get("calendar_upcoming_events") is not None:
        events = _tool(siris, "calendar_upcoming_events", days_ahead=1) or []
    else:
        events = _tool(siris, "home_assistant_get_calendar_events", days=1) or []
    out = _events(events)
    out.sort(key=lambda e: (not e.all_day, e.start or ""))
    return out


MAX_CALENDAR_DAYS = 62


def calendar_range(siris: "SirisAI", start: date, end: date) -> c.CalendarRange:
    """Every event from `start` up to (not including) `end`, local dates, in
    time order. Hidden in guest mode; `unavailable` when there's no calendar
    or every calendar failed."""
    on, _ = _guest(siris)
    base = c.CalendarRange(start=start.isoformat(), end=end.isoformat(), guest_mode=on, events=None)
    if on:
        return base
    if siris.calendar is None:
        return base.model_copy(update={"unavailable": True})
    tz = ZoneInfo(siris.settings.timezone)
    try:
        raw = siris.calendar.events_between(datetime.combine(start, time.min, tz), datetime.combine(end, time.min, tz))
    except Exception as exc:  # noqa: BLE001 - reported as unavailable, never a 500
        logger.warning("Calendar range unavailable: %s", exc)
        return base.model_copy(update={"unavailable": True})
    return base.model_copy(update={"events": _events(raw)})


def todo(siris: "SirisAI") -> list[c.TodoItem]:
    lists = _tool(siris, "home_assistant_get_todo_items") or []
    out = []
    for lst in lists:
        for item in lst.get("items") or []:
            if item.get("summary"):
                out.append(c.TodoItem(summary=str(item["summary"]), list=lst.get("list"), due=item.get("due")))
    return out


def email(siris: "SirisAI") -> c.EmailSummary:
    messages = _tool(siris, "email_list_unread", since_days=1) or []
    important = []
    for m in messages:
        if not m.get("important"):
            continue
        sender = str(m.get("from") or "")
        name = sender.split("<")[0].strip().strip('"') or sender
        important.append(c.ImportantEmail(sender=name, subject=str(m.get("subject") or "(no subject)"), why=[str(w) for w in m.get("why") or []]))
    return c.EmailSummary(unread=len(messages), important=important[:5])


def health(siris: "SirisAI") -> c.HealthSummary | None:
    result = _tool(siris, "health_summary") or {}
    metrics = {
        name: c.HealthMetric(**{k: v for k, v in (m or {}).items() if k in c.HealthMetric.model_fields})
        for name, m in (result.get("metrics") or {}).items()
    }
    if not metrics and not result.get("notes"):
        return None
    return c.HealthSummary(metrics=metrics, notes=[str(n) for n in result.get("notes") or []])


def car(siris: "SirisAI") -> c.CarSummary:
    s = _tool(siris, "car_status")
    return c.CarSummary(
        name=str(s.get("name") or "the car"),
        summary=str(s.get("summary") or ""),
        battery_percent=s.get("battery"),
        range=s.get("range"),
        range_unit=s.get("range_unit"),
        charging=s.get("charging"),
        plugged_in=s.get("plugged_in"),
        locked=s.get("locked"),
        location=s.get("location"),
        low_battery=bool(s.get("low_battery")),
    )


def parcels(siris: "SirisAI") -> list[c.ParcelSummary] | None:
    active = siris.parcels.active()
    if not active and siris.parcel_tracker is None:
        raise _Missing("parcels")
    return [
        c.ParcelSummary(
            id=p["id"], label=str(p["label"]), status=p.get("status"), status_text=p.get("status_text"),
            carrier=p.get("carrier"), eta=p.get("eta"), last_event=p.get("last_event"),
        )
        for p in active
    ]


def energy(siris: "SirisAI") -> c.EnergySummary:
    from app.energy.service import status

    s = status(siris)
    if not s.get("configured"):
        raise _Missing("energy")
    return c.EnergySummary(**{k: s.get(k) for k in ("configured", "cheap_now", "solar_watts", "price", "next_cheap_window")},
                           reasons=list(s.get("reasons") or []), waiting_jobs=len(siris.deferred_jobs.list("waiting")))


def brain(siris: "SirisAI") -> c.BrainSummary:
    if siris.second_brain is None:
        raise _Missing("brain")
    i = siris.second_brain.insights(days=7)
    return c.BrainSummary(
        inbox=int((i.get("summary") or {}).get("inbox") or 0),
        overdue_tasks=[
            c.OverdueTask(task=str(t.get("task")), note=t.get("note"), days_late=t.get("days_late"))
            for t in i.get("overdue_tasks") or [] if t.get("task")
        ],
        deadlines=[c.Deadline(title=str(d.get("title")), days_left=int(d.get("days_left", 0))) for d in i.get("deadlines") or [] if d.get("title")],
        auto_linked=len(i.get("auto_linked") or []),
        pending_auto_links=int(i.get("pending_auto_links") or 0),
        unsure_links=len(i.get("suggested_links") or []),
        highlights=[str(h) for h in (i.get("highlights") or [])[:5]],
        topics=[str(t["tag"]) for t in i.get("topics") or [] if isinstance(t, dict) and t.get("tag")],
    )


def protocols(siris: "SirisAI") -> list[c.ProtocolInfo]:
    from app.protocols import service

    if siris.tools.get("run_protocol") is None:
        raise _Missing("protocols")
    return [
        c.ProtocolInfo(name=p["name"], description=p.get("description") or "", builtin=bool(p.get("builtin")),
                       steps=[s.get("label") or s["tool"] for s in p["steps"]])
        for p in service.all_protocols(siris).values()
    ]


def last_protocol(siris: "SirisAI") -> c.LastProtocolRun | None:
    from app.protocols import service

    last = siris.state.get(service.LAST_RUN_KEY)
    if not last:
        return None
    return c.LastProtocolRun(protocol=last.get("protocol"), at=last.get("ran_at"), undone=bool(last.get("undone")),
                             can_undo=bool(last.get("undo")) and not last.get("undone"))


def cameras(siris: "SirisAI") -> list[str]:
    if siris._frigate_client is None:
        raise _Missing("cameras")
    found = siris._frigate_client.list_cameras()
    return [str(cam.get("name") if isinstance(cam, dict) else cam) for cam in found]


def autonomy(siris: "SirisAI") -> c.Autonomy:
    from app.audit.autonomy import timeline

    t = timeline(siris, hours=24)
    return c.Autonomy(
        summary=t["summary"],
        items=[c.AutonomyItem(**{k: i.get(k) for k in c.AutonomyItem.model_fields if i.get(k) is not None}) for i in t["items"][:12]],
    )


# -- responses ----------------------------------------------------------------------


def info(siris: "SirisAI") -> c.HubInfo:
    tools = siris.tools
    state = guest.status(siris)
    features = {
        "weather": tools.get("weather_forecast") is not None,
        "calendar": tools.get("calendar_upcoming_events") is not None or tools.get("home_assistant_get_calendar_events") is not None,
        "todo": tools.get("home_assistant_get_todo_items") is not None,
        "email": tools.get("email_list_unread") is not None,
        "health": tools.get("health_summary") is not None,
        "car": tools.get("car_status") is not None,
        "parcels": siris.parcel_tracker is not None,
        "energy": bool(siris.settings.energy_cheap_windows or siris.settings.energy_solar_entity_id or siris.settings.energy_price_entity_id),
        "cameras": siris._frigate_client is not None,
        "vision": tools.get("frigate_look_at_camera") is not None,
        "protocols": tools.get("run_protocol") is not None,
        "second_brain": siris.second_brain is not None,
        "voice_stt": siris.settings.stt_provider not in ("", "none"),
        "voice_tts": siris.settings.tts_provider not in ("", "none"),
        "sirisos": bool(siris.settings.sirisos_url),
    }
    return c.HubInfo(
        user=get_current_user_id(),
        guest_mode=bool(state.get("on")),
        guest_until=state.get("until") if state.get("on") else None,
        features=features,
        personal_sections=list(PERSONAL_SECTIONS),
    )


def brief(siris: "SirisAI") -> c.Brief:
    on, hidden = _guest(siris)
    sections, unavailable = _sections(
        siris,
        {
            "weather": lambda: weather(siris),
            "calendar": lambda: calendar(siris),
            "todo": lambda: todo(siris),
            "email": lambda: email(siris),
            "health": lambda: health(siris),
            "car": lambda: car(siris),
            "parcels": lambda: parcels(siris),
            "energy": lambda: energy(siris),
            "brain": lambda: brain(siris),
        },
        hidden,
    )
    return c.Brief(
        generated_at=clock.now_local(siris.settings.timezone).isoformat(),
        guest_mode=on,
        hidden=hidden,
        attention=c.AttentionCounts(**siris.attention.counts()),
        unavailable=unavailable,
        **sections,
    )


def widgets(siris: "SirisAI") -> c.Widgets:
    on, hidden = _guest(siris)
    sections, unavailable = _sections(
        siris,
        {
            "car": lambda: car(siris),
            "parcels": lambda: parcels(siris),
            "energy": lambda: energy(siris),
            "protocols": lambda: protocols(siris),
            "last_protocol": lambda: last_protocol(siris),
            "cameras": lambda: cameras(siris),
            "autonomy": lambda: autonomy(siris),
        },
        hidden,
    )
    return c.Widgets(
        generated_at=clock.now_local(siris.settings.timezone).isoformat(),
        guest_mode=on,
        hidden=hidden,
        attention=c.AttentionCounts(**siris.attention.counts()),
        unavailable=unavailable,
        **{k: (v if v is not None else ([] if k in ("protocols", "cameras") else None)) for k, v in sections.items()},
    )


def search(siris: "SirisAI", query: str, limit: int = 8) -> c.SearchResults:
    """Notes, past chats and memories matching `query`. In guest mode only
    nothing personal comes back, so this returns no hits at all."""
    if guest.is_on(siris):
        return c.SearchResults(query=query, hits=[], unavailable=[])

    def notes() -> list[c.SearchHit]:
        if siris.second_brain is None:
            raise _Missing("notes")
        return [
            c.SearchHit(kind="note", title=str(n.get("title")), snippet=next(iter(n.get("excerpts") or []), "") or "",
                        ref=str(n.get("title")), note_type=n.get("type"))
            for n in siris.second_brain.search(query, limit=limit)
        ]

    def chats() -> list[c.SearchHit]:
        seen: set[str] = set()
        hits = []
        for m in siris.conversation.search_messages(query, limit=limit * 3):
            cid = str(m.get("conversation_id") or "")
            if not cid or cid in seen:
                continue
            seen.add(cid)
            text = str(m.get("text") or "")
            who = "You said" if m.get("role") == "user" else "Siris said"
            hits.append(c.SearchHit(kind="chat", title=text[:120], snippet=who, ref=cid, when=str(m.get("when") or "") or None))
        return hits[:limit]

    def memories() -> list[c.SearchHit]:
        return [
            c.SearchHit(kind="memory", title=str(m.get("content"))[:160], ref=str(m.get("id")), when=m.get("created_at"))
            for m in siris.memory.recall(query, limit=limit)
        ]

    sections, unavailable = _sections(siris, {"notes": notes, "chats": chats, "memories": memories}, [])
    hits = [h for name in ("notes", "chats", "memories") for h in (sections[name] or [])]
    return c.SearchResults(query=query, hits=hits, unavailable=unavailable)


# -- the day: presence, reminders, email with people, upkeep, health by day -------


def presence(siris: "SirisAI") -> c.Presence:
    """Home or away from the last UniFi presence check (none when it isn't set up)."""
    from app.skills.builtin.presence import _PRESENCE_ENTITY_NAME, anyone_home

    last = siris.events.recent_events(entity_name=_PRESENCE_ENTITY_NAME, limit=1)
    checked = last[0].get("created_at") if last else None
    return c.Presence(home=anyone_home(siris), checked_at=str(checked) if checked else None)


def reminders(siris: "SirisAI") -> c.ReminderList:
    on, _ = _guest(siris)
    if on:
        return c.ReminderList(guest_mode=True)
    rows = siris.reminders.list_pending()
    return c.ReminderList(guest_mode=False, reminders=[
        c.Reminder(id=r["id"], message=r["message"], due_at=r["due_at"] + "Z" if "+" not in r["due_at"] else r["due_at"], status="pending")
        for r in rows
    ])


def email_with(siris: "SirisAI", addresses: list[str], limit: int) -> c.EmailWith:
    on, _ = _guest(siris)
    if on:
        return c.EmailWith(guest_mode=True, configured=siris.tools.get("email_read") is not None)
    tool = siris.tools.get("email_read")
    if tool is None:
        return c.EmailWith(guest_mode=False, configured=False)
    found = tool._client.recent_with(addresses, limit=limit)  # type: ignore[attr-defined]
    return c.EmailWith(guest_mode=False, configured=True, messages=[
        c.EmailBrief(uid=str(m["uid"]), sender=_who(str(m.get("from") or "")), subject=str(m.get("subject") or "(no subject)"),
                     date=m.get("date") or None, snippet=str(m.get("snippet") or ""))
        for m in found
    ])


def email_about(siris: "SirisAI", words: list[str], limit: int) -> c.EmailWith:
    """The latest emails with any of `words` in the subject (a SirisOS project page)."""
    on, _ = _guest(siris)
    if on:
        return c.EmailWith(guest_mode=True, configured=siris.tools.get("email_read") is not None)
    tool = siris.tools.get("email_read")
    if tool is None:
        return c.EmailWith(guest_mode=False, configured=False)
    found = tool._client.recent_about(words, limit=limit)  # type: ignore[attr-defined]
    return c.EmailWith(guest_mode=False, configured=True, messages=[
        c.EmailBrief(uid=str(m["uid"]), sender=_who(str(m.get("from") or "")), subject=str(m.get("subject") or "(no subject)"),
                     date=m.get("date") or None, snippet=str(m.get("snippet") or ""))
        for m in found
    ])


def _who(sender: str) -> str:
    from email.utils import parseaddr

    name, address = parseaddr(sender)
    return name or address or sender


def upkeep(siris: "SirisAI") -> c.Upkeep:
    from app import __version__
    from app.upkeep import backups, updates

    found = None
    if backups.db_path(siris.settings) is not None:
        items = backups.list_backups(siris.settings)
        found = c.BackupStatus(newest=items[0]["at"] if items else None, count=len(items), stale=backups.freshness(siris) is not None)
    offered = [i for i in siris.attention.list("open") if i.get("source") == updates.SOURCE and str(i.get("key", "")).startswith("update:")]
    latest = offered[0]["key"].split(":", 1)[1] if offered else None
    return c.Upkeep(backups=found, update=c.UpdateStatus(current=__version__, latest=latest, available=bool(latest)))


def health_days(siris: "SirisAI", days: int) -> c.HealthDays:
    from datetime import timedelta

    from app.health.service import METRICS

    on, _ = _guest(siris)
    if on:
        return c.HealthDays(guest_mode=True)
    today = clock.now_local(siris.settings.timezone).date()
    since = (today - timedelta(days=days - 1)).isoformat()
    by_day: dict[str, dict[str, float]] = {(today - timedelta(days=i)).isoformat(): {} for i in range(days)}
    for metric in METRICS:
        for day, value in siris.health.series(metric, since):
            if day in by_day:
                by_day[day][metric] = value
    return c.HealthDays(guest_mode=False, days=[c.HealthDay(date=d, metrics=m) for d, m in sorted(by_day.items())])


# -- home controls (SirisOS 4.2) ---------------------------------------------------

CONTROL_DOMAINS = {"light": "light", "switch": "switch", "cover": "garage", "lock": "lock"}
MAX_CONTROLS = 16


def _control_kind(state: dict) -> str | None:
    domain = state["entity_id"].split(".", 1)[0]
    if domain == "cover":
        attrs = state.get("attributes") or {}
        named = str(attrs.get("friendly_name") or state["entity_id"]).lower()
        return "garage" if attrs.get("device_class") in ("garage", "gate") or "garage" in named or "gate" in named else None
    return CONTROL_DOMAINS.get(domain)


def _control(state: dict) -> c.Control | None:
    kind = _control_kind(state)
    if kind is None:
        return None
    attrs = state.get("attributes") or {}
    name = str(attrs.get("friendly_name") or state["entity_id"])
    brightness = attrs.get("brightness")
    confirm = None
    if kind == "garage":
        confirm = f"{'Close' if state['state'] == 'open' else 'Open'} {name}?"
    elif kind == "lock":
        confirm = f"Unlock {name}?" if state["state"] == "locked" else None
    return c.Control(entity_id=state["entity_id"], name=name, kind=kind, state=str(state["state"]),  # type: ignore[arg-type]
                     brightness_pct=round(brightness / 255 * 100) if isinstance(brightness, (int, float)) and state["state"] == "on" else None,
                     confirm=confirm)


def controls(siris: "SirisAI") -> c.Controls:
    """Lights, switches, garage doors and locks, and scenes, for SirisOS's Home.
    In guest mode garage doors and locks are left out."""
    client = siris._home_assistant_client
    on = guest.is_on(siris)
    scenes = [c.SceneControl(id=f"siris:{s['id']}", name=s["name"], source="siris") for s in siris.scenes.list_scenes()][:12]
    if client is None:
        return c.Controls(configured=False, guest_mode=on, scenes=scenes)
    states = {s["entity_id"]: s for s in client.list_states()}
    wanted = [e.strip() for e in siris.settings.hub_controls.split(",") if e.strip()]
    if wanted:
        picked = [states[e] for e in wanted if e in states]
    else:
        picked = sorted((s for s in states.values() if _control_kind(s) in ("light", "garage", "lock")),
                        key=lambda s: (["garage", "lock", "light"].index(_control_kind(s) or "light"), str((s.get("attributes") or {}).get("friendly_name") or s["entity_id"]).lower()))
    found = [ctl for s in picked if (ctl := _control(s)) is not None and not (on and ctl.kind in ("garage", "lock"))][:MAX_CONTROLS]
    ha_scenes = sorted((s for s in states.values() if s["entity_id"].startswith("scene.")), key=lambda s: str((s.get("attributes") or {}).get("friendly_name") or s["entity_id"]).lower())
    scenes += [c.SceneControl(id=s["entity_id"], name=str((s.get("attributes") or {}).get("friendly_name") or s["entity_id"]), source="home_assistant") for s in ha_scenes[:12]]
    return c.Controls(configured=True, guest_mode=on, controls=found, scenes=scenes)


ACTIONS = {
    "light": {"toggle": "toggle", "turn_on": "turn_on", "turn_off": "turn_off"},
    "switch": {"toggle": "toggle", "turn_on": "turn_on", "turn_off": "turn_off"},
    "garage": {"open": "open_cover", "close": "close_cover"},
    "lock": {"lock": "lock", "unlock": "unlock"},
}


class ControlRefused(Exception):
    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.status = status


def control(siris: "SirisAI", entity_id: str, action: str, confirmed: bool, trigger: str) -> c.ControlResult:
    """Do one thing to one entity. Only entities `controls` would show, only
    their own actions, and garage doors and unlocking only when confirmed."""
    client = siris._home_assistant_client
    if client is None:
        raise ControlRefused("Home Assistant isn't set up in SirisAI.", 404)
    state = client.get_state_or_none(entity_id)
    ctl = _control(state) if state else None
    if ctl is None or entity_id not in {x.entity_id for x in controls(siris).controls}:
        raise ControlRefused(f"{entity_id} isn't one of the Home controls.", 404)
    service = ACTIONS[ctl.kind].get(action)
    if service is None:
        raise ControlRefused(f"{ctl.name} can't {action.replace('_', ' ')}.", 422)
    if (ctl.kind == "garage" or action == "unlock") and not confirmed:
        raise ControlRefused(f"{'Garage doors' if ctl.kind == 'garage' else 'Unlocking'} needs a yes first.", 409)
    siris.run_tool("home_assistant_call_service", {"domain": entity_id.split(".", 1)[0], "service": service, "entity_id": entity_id},
                   confirmed=True, trigger=trigger)
    after = client.get_state_or_none(entity_id)
    return c.ControlResult(entity_id=entity_id, action=action, state=str(after["state"]) if after else None)


def run_scene(siris: "SirisAI", scene_id: str, trigger: str) -> c.ControlResult:
    if scene_id.startswith("siris:") and scene_id[6:].isdigit():
        if siris.scenes.get_scene(int(scene_id[6:])) is None:
            raise ControlRefused("No such scene.", 404)
        siris.run_tool("run_scene", {"scene_id": int(scene_id[6:])}, confirmed=True, trigger=trigger)
        return c.ControlResult(entity_id=scene_id, action="run")
    if scene_id.startswith("scene.") and siris._home_assistant_client is not None:
        if siris._home_assistant_client.get_state_or_none(scene_id) is None:
            raise ControlRefused("No such scene.", 404)
        siris.run_tool("home_assistant_call_service", {"domain": "scene", "service": "turn_on", "entity_id": scene_id}, confirmed=True, trigger=trigger)
        return c.ControlResult(entity_id=scene_id, action="run")
    raise ControlRefused("No such scene.", 404)
