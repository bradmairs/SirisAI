"""Travel mode (SirisAI 4.6): a trip in the calendar gets the house and the
bag ready.

**Finding trips**: events in the next SIRISAI_TRAVEL_LOOKAHEAD_DAYS (21)
that say so ("Flight to Sydney", "Trip", "Holiday", "Hotel", "Airbnb",
"Conference", ✈) or that last two days or more somewhere else. The
destination is the event's location, or what follows "to" in its title.

**Before a trip** (SIRISAI_TRAVEL_NOTICE_DAYS, 3, ahead, at
SIRISAI_TRAVEL_HOUR_LOCAL, 6pm), the attention feed gets one item:

- the weather where you're going, for the days you're there (Open-Meteo,
  when the place can be found and the days are within its 16-day forecast);
- a packing list that follows that weather and the length of the trip;
- a house list: bins, plants, parcels to hold or redirect, a key for
  whoever's minding the place;
- **Away while I'm gone**: while the trip lasts, away mode runs the away
  protocol by itself when everyone has left (as SIRISAI_AWAY_MODE=auto
  would), even if it normally asks. Undoing it when you're back still never
  unlocks a door, opens the garage or disarms an alarm;
- **Remind me to pack**: a reminder the evening before you leave, with the
  list.

GET /siris/travel lists the trips coming up; the travel_upcoming tool
answers "when am I away next" and "what should I pack".
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any

import httpx

from app.core import clock
from app.hub.attention import action, register_action

if TYPE_CHECKING:
    from app.core.siris import SirisAI

logger = logging.getLogger(__name__)

AWAY_KEY = "travel:away"  # {start, end, trip}
_TRIP_WORDS = re.compile(r"\b(flight|fly(?:ing)? to|trip|holiday|vacation|travel|hotel|airbnb|conference|getaway|camping)\b|✈", re.I)
_TO_PLACE = re.compile(r"\bto\s+([A-Z][\w'-]+(?:\s+[A-Z][\w'-]+){0,2})")
GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
transport: httpx.BaseTransport | None = None


def _parse(value: str | None, zone: Any) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    if len(value) <= 10:  # an all-day event
        parsed = datetime.combine(parsed.date(), time(0))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=zone)


def find_trips(siris: "SirisAI", now: datetime | None = None, days: int | None = None) -> list[dict[str, Any]]:
    calendar = getattr(siris, "calendar", None)
    if calendar is None:
        return []
    zone = clock.get_zone(siris.settings.timezone)
    now = (now or datetime.now(UTC)).astimezone(zone)
    horizon = now + timedelta(days=days or siris.settings.travel_lookahead_days)
    home = (siris.settings.travel_home_city or "").lower()
    trips = []
    for event in calendar.events_between(now - timedelta(days=1), horizon):
        start, end = _parse(event.get("start"), zone), _parse(event.get("end"), zone)
        if start is None:
            continue
        end = end or start
        title = event.get("summary") or ""
        location = (event.get("location") or "").strip()
        long_away = (end - start) >= timedelta(days=2) and location and (not home or home not in location.lower())
        if not (_TRIP_WORDS.search(title) or long_away):
            continue
        place = location.split(",")[0].strip() if location else ""
        to = _TO_PLACE.search(title)
        if to and (not place or len(place) > 40):
            place = to.group(1)
        if end < now:
            continue
        trips.append({
            "title": title, "destination": place or title, "location": location,
            "start": start.isoformat(), "end": end.isoformat(),
            "nights": max(0, (end.date() - start.date()).days), "key": f"{start.date().isoformat()}:{(place or title).lower()[:40]}",
        })
    # One trip per start day and place (a flight and the hotel are the same trip).
    merged: dict[str, dict[str, Any]] = {}
    for trip in sorted(trips, key=lambda t: t["start"]):
        key = trip["start"][:10]
        if key in merged:
            first = merged[key]
            first["end"] = max(first["end"], trip["end"])
            first["nights"] = max(first["nights"], trip["nights"])
            first["title"] = f"{first['title']}; {trip['title']}"
            continue
        merged[key] = trip
    return list(merged.values())


def weather_for(siris: "SirisAI", place: str, start: date, end: date) -> list[dict[str, Any]]:
    """The forecast where you're going, for the trip's days (within 16 days)."""
    today = clock.now_local(siris.settings.timezone).date()
    if not place or start > today + timedelta(days=15):
        return []
    with httpx.Client(timeout=10, transport=transport) as http:
        found = http.get(GEOCODE_URL, params={"name": place, "count": 1}).json().get("results") or []
        if not found:
            return []
        spot = found[0]
        response = http.get(
            siris.settings.weather_base_url.rstrip("/") + "/v1/forecast",
            params={
                "latitude": spot["latitude"], "longitude": spot["longitude"], "timezone": "auto",
                "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max",
                "start_date": max(start, today).isoformat(), "end_date": min(end, today + timedelta(days=15)).isoformat(),
            },
        ).json().get("daily", {})
    return [
        {"date": d, "high_c": hi, "low_c": lo, "rain_chance_percent": rain}
        for d, hi, lo, rain in zip(response.get("time", []), response.get("temperature_2m_max", []),
                                   response.get("temperature_2m_min", []), response.get("precipitation_probability_max", []))
    ]


def packing_list(trip: dict[str, Any], weather: list[dict[str, Any]]) -> list[str]:
    nights = max(1, trip.get("nights") or 1)
    items = ["Phone charger", "Wallet and cards", "Keys", "Toiletries", "Medication",
             f"Clothes for {nights + 1} day{'s' if nights else ''}", f"{min(nights + 1, 7)} sets of underwear and socks", "Sleepwear"]
    if re.search(r"flight|fly|✈|airport", trip["title"], re.I):
        items += ["ID or passport", "Boarding pass", "Headphones"]
    if weather:
        high = max((w["high_c"] for w in weather if w["high_c"] is not None), default=None)
        low = min((w["low_c"] for w in weather if w["low_c"] is not None), default=None)
        rain = max((w["rain_chance_percent"] or 0 for w in weather), default=0)
        if rain >= 40:
            items.append("Umbrella or rain jacket")
        if low is not None and low < 10:
            items += ["Warm jacket", "Beanie"]
        if high is not None and high >= 26:
            items += ["Sunscreen", "Hat", "Sunglasses"]
    if nights >= 5:
        items.append("Laundry bag")
    return items


HOUSE_LIST = [
    "Bins out before you go, if collection falls while you're away",
    "Water the plants, or ask someone to",
    "Hold or redirect parcels due while you're away",
    "A key for whoever's minding the place",
    "Fridge: use up or bin what won't last",
]


def plan(siris: "SirisAI", trip: dict[str, Any]) -> dict[str, Any]:
    start, end = date.fromisoformat(trip["start"][:10]), date.fromisoformat(trip["end"][:10])
    try:
        weather = weather_for(siris, trip["destination"], start, end)
    except Exception:  # noqa: BLE001 - the trip is still worth planning without it
        logger.info("No forecast for %s", trip["destination"], exc_info=True)
        weather = []
    return {**trip, "weather": weather, "packing": packing_list(trip, weather), "house": HOUSE_LIST}


def upcoming(siris: "SirisAI", now: datetime | None = None) -> list[dict[str, Any]]:
    away = siris.state.get(AWAY_KEY) or {}
    out = []
    for trip in find_trips(siris, now):
        planned = plan(siris, trip)
        planned["away_mode"] = away.get("trip") == trip["key"]
        out.append(planned)
    return out


def _describe_weather(weather: list[dict[str, Any]]) -> str:
    if not weather:
        return ""
    highs = [w["high_c"] for w in weather if w["high_c"] is not None]
    lows = [w["low_c"] for w in weather if w["low_c"] is not None]
    rain = max((w["rain_chance_percent"] or 0 for w in weather), default=0)
    return f"Weather there: {min(lows):.0f} to {max(highs):.0f}°C, rain chance up to {rain}%. " if highs and lows else ""


def check(siris: "SirisAI", now: datetime | None = None) -> list[dict[str, Any]]:
    zone = clock.get_zone(siris.settings.timezone)
    local_now = (now or datetime.now(UTC)).astimezone(zone)
    soon = local_now + timedelta(days=siris.settings.travel_notice_days)
    wanted = []
    for trip in find_trips(siris, now):
        start = datetime.fromisoformat(trip["start"])
        if start > soon or datetime.fromisoformat(trip["end"]) < local_now:
            continue
        planned = plan(siris, trip)
        when = start.strftime("%A %-d %B")
        wanted.append({
            "key": f"travel:{trip['key']}", "kind": "suggestion",
            "title": f"{trip['destination']} on {when}" + (f", {trip['nights']} night{'s' if trip['nights'] != 1 else ''}" if trip["nights"] else ""),
            "severity": "info",
            "body": _describe_weather(planned["weather"]) + "Pack: " + ", ".join(planned["packing"]) + ". House: " + "; ".join(HOUSE_LIST) + ".",
            "actions": [action("away", "Away while I'm gone", "primary"), action("pack", "Remind me to pack"), action("dismiss", "Not now")],
            "payload": {"trip": trip, "packing": planned["packing"]},
        })
    siris.attention.sync("travel", wanted, outcome="Trip's over")
    return wanted


def away_active(siris: "SirisAI", now: datetime | None = None) -> bool:
    """Whether a trip with "Away while I'm gone" is on now (household/away.py)."""
    away = siris.state.get(AWAY_KEY) or {}
    if not away:
        return False
    now = now or datetime.now(UTC)
    try:
        return datetime.fromisoformat(away["start"]) - timedelta(hours=12) <= now <= datetime.fromisoformat(away["end"]) + timedelta(hours=12)
    except (KeyError, ValueError):
        return False


def set_away(siris: "SirisAI", trip: dict[str, Any]) -> dict[str, Any]:
    siris.state.set(AWAY_KEY, {"start": trip["start"], "end": trip["end"], "trip": trip["key"]})
    return {"away_mode": "automatic", "until": trip["end"]}


def _away(siris: "SirisAI", item: dict[str, Any], payload: dict[str, Any], trigger: str) -> tuple[Any, str]:
    return set_away(siris, payload["trip"]), "Away mode runs by itself while you're away"


def _pack(siris: "SirisAI", item: dict[str, Any], payload: dict[str, Any], trigger: str) -> tuple[Any, str]:
    trip = payload["trip"]
    zone = clock.get_zone(siris.settings.timezone)
    start = datetime.fromisoformat(trip["start"]).astimezone(zone)
    at = datetime.combine(start.date() - timedelta(days=1), time(19), tzinfo=zone)
    if at < datetime.now(zone):
        at = datetime.now(zone) + timedelta(minutes=5)
    reminder = siris.reminders.create(f"Pack for {trip['destination']}: " + ", ".join(payload.get("packing") or []), at.astimezone(UTC))
    return reminder, "Packing reminder set for " + at.strftime("%a %-I%p").replace("AM", "am").replace("PM", "pm")


register_action("travel", "away", _away)
register_action("travel", "pack", _pack)
