"""7.3: what SirisOS's weekend planner, garden reminder, "what to watch tonight", container updates, internet
history and bedroom check read from SirisAI. Each says when its source isn't set up instead of failing."""

from __future__ import annotations

import logging
import re
import threading
from datetime import datetime
from typing import TYPE_CHECKING, Any

from app.hub import compose
from app.hub import contract as c

if TYPE_CHECKING:
    from app.core.siris import SirisAI

logger = logging.getLogger(__name__)
_ENTITY = re.compile(r"(sensor|binary_sensor|climate|weather)\.[a-z0-9_]{1,100}")


def forecast_hourly(siris: "SirisAI", days: int = 4, past_days: int = 0) -> c.HourlyForecast:
    weather = getattr(siris, "_weather_client", None)
    if weather is None:
        return c.HourlyForecast(unavailable=True)
    try:
        raw = weather.hourly(days=days, past_days=past_days, timezone=siris.settings.timezone or "auto")
    except Exception as exc:  # noqa: BLE001 - reported as unavailable, never a 500
        logger.warning("Hourly forecast unavailable: %s", exc)
        return c.HourlyForecast(unavailable=True)
    from app.core import clock

    now = clock.now_local(siris.settings.timezone).strftime("%Y-%m-%dT%H:%M")  # Open-Meteo's local times, compared as text
    past = sum(h.get("rain_mm") or 0 for h in raw["hours"] if h["time"] < now) if past_days else 0.0
    guest = compose._guest(siris)[0]  # noqa: SLF001
    return c.HourlyForecast(latitude=None if guest else raw["latitude"], longitude=None if guest else raw["longitude"],
                            hours=[c.HourlyWeather(**h) for h in raw["hours"] if h["time"] >= now[:13]],
                            sun=[c.SunTimes(**s) for s in raw["sun"]], past_rain_mm=round(past, 1))


def _item(m: dict[str, Any]) -> c.WatchItem:
    return c.WatchItem(title=m.get("title") or "", type=m.get("type"), show=m.get("show"), year=m.get("year"),
                       genres=[g for g in m.get("genres") or [] if g], rating=m.get("rating"), summary=m.get("summary") or "",
                       added_at=m.get("added_at"))


def watch(siris: "SirisAI", days: int = 7) -> c.Watch:
    if compose._guest(siris)[0]:  # noqa: SLF001
        return c.Watch(guest_mode=True)
    out = c.Watch()
    plex = getattr(siris, "_plex_client", None)
    if plex is not None:
        try:
            out.on_deck = [_item(m) for m in plex.on_deck()][:12]
            fresh = [m for m in plex.recently_added(60) if not m.get("watched")]
            seen: set[str] = set()
            new: list[c.WatchItem] = []
            for m in fresh:  # one line per show, not every episode
                key = m.get("show") or m.get("title") or ""
                if key in seen:
                    continue
                seen.add(key)
                new.append(_item(m))
            out.new_unwatched = new[:12]
        except Exception as exc:  # noqa: BLE001
            logger.warning("Plex unavailable for watch: %s", exc)
    sonarr = getattr(siris, "_sonarr_client", None)
    if sonarr is not None:
        try:
            out.episodes = [c.UpcomingEpisode(series=e.get("series_title"), episode=e.get("episode_title"), season=e.get("season_number"),
                                              number=e.get("episode_number"), air_date=e.get("air_date")) for e in sonarr.calendar(days)]
        except Exception as exc:  # noqa: BLE001
            logger.warning("Sonarr unavailable for watch: %s", exc)
    radarr = getattr(siris, "_radarr_client", None)
    if radarr is not None:
        try:
            movies = []
            for m in radarr.calendar(days):
                kind, when = next(((k, m.get(f)) for k, f in (("digital", "digital_release"), ("physical", "physical_release"), ("cinema", "in_cinemas"))
                                   if m.get(f)), (None, None))
                movies.append(c.UpcomingMovie(title=m.get("title"), release=(when or "")[:10] or None, kind=kind))
            out.movies = movies
        except Exception as exc:  # noqa: BLE001
            logger.warning("Radarr unavailable for watch: %s", exc)
    return out


def containers(siris: "SirisAI") -> c.ContainerUpdates:
    from app.homelab import containers as homelab

    if getattr(siris, "_docker_client", None) is None:
        return c.ContainerUpdates(available=False)
    report = siris.state.get(homelab.STATE_KEY) or {}
    running = homelab._running.locked()  # noqa: SLF001
    return c.ContainerUpdates(available=True, checked_at=report.get("checked_at"), running=running,
                              updates=[c.ContainerUpdate(**{k: u.get(k) for k in ("container", "image", "current", "available", "notes")})
                                       for u in report.get("updates") or []],
                              up_to_date=report.get("up_to_date") or [], skipped=report.get("skipped") or {})


_checking = threading.Lock()


def check_containers(siris: "SirisAI") -> c.ContainerUpdates:
    from app.homelab import containers as homelab

    if getattr(siris, "_docker_client", None) is None:
        return c.ContainerUpdates(available=False)
    with _checking:
        homelab.check(siris)
    return containers(siris)


def update_containers(siris: "SirisAI", names: list[str] | None) -> c.ContainerUpdateStarted:
    """Update the named containers (or every one with an update), in the background, the way the feed's button does."""
    from app.homelab import containers as homelab

    report = siris.state.get(homelab.STATE_KEY) or {}
    known = [u["container"] for u in report.get("updates") or []]
    chosen = [n for n in (names or known) if n in known]
    if not chosen:
        return c.ContainerUpdateStarted(started=0, message="Nothing to update")
    result, message = homelab._update_action(siris, {}, {"containers": chosen}, "SirisOS: Update")  # noqa: SLF001
    return c.ContainerUpdateStarted(started=result.get("started", 0), order=result.get("order") or [], message=message)


def internet(siris: "SirisAI", days: float = 30) -> c.Internet:
    from datetime import UTC, timedelta

    from app.homelab import internet as net

    state = net._state(siris)  # noqa: SLF001
    if not siris.settings.internet_probe_seconds and not siris.settings.internet_speedtest_hours and not state["speeds"] and not state["outages"]:
        return c.Internet(available=False, days=days)
    s = net.summary(siris, days)
    since = datetime.now(UTC) - timedelta(days=days)
    speeds = [c.SpeedSample(**sp) for sp in state["speeds"] if datetime.fromisoformat(sp["at"]) >= since]
    return c.Internet(days=days, speeds=speeds, outages=[c.Outage(**o) for o in s["outages"]],
                      average_down_mbps=s["average_down_mbps"], average_up_mbps=s["average_up_mbps"], minutes_down=s["minutes_down"],
                      down_now_since=s["down_now_since"], text=s["text"])


def sensors(siris: "SirisAI", ids: list[str]) -> c.SensorStates:
    if compose._guest(siris)[0]:  # noqa: SLF001
        return c.SensorStates(guest_mode=True, sensors=[])
    ha = getattr(siris, "_home_assistant_client", None)
    if ha is None:
        return c.SensorStates(available=False)
    out = []
    for eid in [i for i in ids if _ENTITY.fullmatch(i)][:10]:
        try:
            s = ha.get_state_or_none(eid)
        except Exception:  # noqa: BLE001
            s = None
        if not s:
            out.append(c.SensorState(entity_id=eid))
            continue
        attrs = s.get("attributes") or {}
        out.append(c.SensorState(entity_id=eid, name=attrs.get("friendly_name"), state=str(s.get("state")),
                                 unit=attrs.get("unit_of_measurement"), device_class=attrs.get("device_class"), updated=s.get("last_updated")))
    return c.SensorStates(sensors=out)
