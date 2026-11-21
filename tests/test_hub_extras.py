"""7.3: the hub's hourly forecast, watch tonight, container updates, internet history and sensors, for SirisOS."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.siris import SirisAI, get_siris
from app.homelab import containers as homelab
from app.main import app
from app.weather.client import WeatherClient

H: dict[str, str] = {}


def _weather() -> WeatherClient:
    def handler(request: httpx.Request) -> httpx.Response:
        p = request.url.params
        assert p["past_days"] == "2" and "precipitation" in p["hourly"]
        start = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - timedelta(hours=48)
        times = [(start + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M") for i in range(48 + 96)]
        return httpx.Response(200, json={
            "hourly": {"time": times, "temperature_2m": [18] * len(times), "precipitation_probability": [10] * len(times),
                       "precipitation": [0.5 if i < 4 else 0 for i in range(len(times))], "weather_code": [1] * len(times),
                       "uv_index": [3] * len(times), "wind_speed_10m": [12] * len(times)},
            "daily": {"time": ["2026-11-21", "2026-11-22"], "sunrise": ["2026-11-21T05:57", "2026-11-22T05:56"], "sunset": ["2026-11-21T20:22", "2026-11-22T20:23"]}})

    return WeatherClient("https://api.open-meteo.com", -37.8, 144.9, transport=httpx.MockTransport(handler))


class FakePlex:
    def on_deck(self):
        return [{"title": "Slow Horses: S04E03", "type": "episode", "show": "Slow Horses", "genres": ["Drama"], "summary": "x"}]

    def recently_added(self, limit=50):
        return [{"title": "The Bear: S03E01", "type": "episode", "show": "The Bear", "watched": False},
                {"title": "The Bear: S03E02", "type": "episode", "show": "The Bear", "watched": False},
                {"title": "Dune: Part Two", "type": "movie", "year": 2024, "watched": False, "genres": ["Sci-Fi"]},
                {"title": "Old film", "type": "movie", "watched": True}]


class FakeSonarr:
    def calendar(self, days_ahead=7):
        return [{"series_title": "Severance", "episode_title": "Hello", "season_number": 2, "episode_number": 3, "air_date": "2026-11-24"}]


class FakeRadarr:
    def calendar(self, days_ahead=7):
        return [{"title": "A Film", "in_cinemas": "2026-11-01", "digital_release": "2026-11-25T00:00:00Z"}]


class FakeHA:
    def get_state_or_none(self, eid):
        if eid == "sensor.bedroom_temperature":
            return {"state": "24.5", "last_updated": "2026-11-21T21:00:00+00:00",
                    "attributes": {"friendly_name": "Bedroom temperature", "unit_of_measurement": "°C", "device_class": "temperature"}}
        return None


def _with(siris: SirisAI):
    app.dependency_overrides[get_siris] = lambda: siris


def test_hourly_forecast_watch_and_sensors(client: TestClient) -> None:
    siris = SirisAI(settings=Settings(timezone="UTC"))
    siris._weather_client = _weather()
    siris._plex_client = FakePlex()
    siris._sonarr_client = FakeSonarr()
    siris._radarr_client = FakeRadarr()
    siris._home_assistant_client = FakeHA()
    _with(siris)
    try:
        f = client.get("/siris/hub/v1/forecast/hourly", params={"days": 4, "past_days": 2}, headers=H).json()
        assert f["latitude"] == -37.8 and f["past_rain_mm"] == 2.0 and f["hours"][0]["temperature_c"] == 18
        assert f["hours"][0]["time"] >= datetime.now(UTC).strftime("%Y-%m-%dT%H")  # from this hour on
        assert f["sun"][0] == {"date": "2026-11-21", "sunrise": "2026-11-21T05:57", "sunset": "2026-11-21T20:22"}
        w = client.get("/siris/hub/v1/watch", headers=H).json()
        assert [i["title"] for i in w["on_deck"]] == ["Slow Horses: S04E03"]
        assert [i["title"] for i in w["new_unwatched"]] == ["The Bear: S03E01", "Dune: Part Two"]  # one line per show, watched left out
        assert w["episodes"][0]["series"] == "Severance" and w["movies"] == [{"title": "A Film", "release": "2026-11-25", "kind": "digital"}]
        s = client.get("/siris/hub/v1/sensors", params={"ids": "sensor.bedroom_temperature,sensor.gone,light.x;rm"}, headers=H).json()
        assert [x["entity_id"] for x in s["sensors"]] == ["sensor.bedroom_temperature", "sensor.gone"]
        assert s["sensors"][0]["state"] == "24.5" and s["sensors"][0]["unit"] == "°C" and s["sensors"][1]["state"] is None
    finally:
        app.dependency_overrides.pop(get_siris, None)


def test_nothing_set_up_says_so(client: TestClient) -> None:
    siris = SirisAI(settings=Settings(timezone="UTC", internet_probe_seconds=0, internet_speedtest_hours=0))
    for name in ("_weather_client", "_plex_client", "_sonarr_client", "_radarr_client", "_docker_client", "_home_assistant_client"):
        setattr(siris, name, None)
    _with(siris)
    try:
        assert client.get("/siris/hub/v1/forecast/hourly", headers=H).json()["unavailable"] is True
        assert client.get("/siris/hub/v1/watch", headers=H).json() == {"guest_mode": False, "on_deck": None, "new_unwatched": None, "episodes": None, "movies": None}
        assert client.get("/siris/hub/v1/containers", headers=H).json()["available"] is False
        assert client.get("/siris/hub/v1/internet", headers=H).json()["available"] is False
        assert client.get("/siris/hub/v1/sensors", params={"ids": "sensor.a"}, headers=H).json()["available"] is False
    finally:
        app.dependency_overrides.pop(get_siris, None)


def test_containers_and_internet(client: TestClient, monkeypatch) -> None:
    siris = SirisAI(settings=Settings(timezone="UTC"))
    siris._docker_client = object()
    siris.state.set(homelab.STATE_KEY, {"checked_at": "2026-11-21T05:00:00+00:00", "up_to_date": ["plex"], "skipped": {"siris": "SirisAI itself"},
                                        "updates": [{"container": "sonarr", "image": "lscr.io/linuxserver/sonarr:latest", "current": "sha256:aaa", "available": "sha256:bbb"}]})
    started = []
    monkeypatch.setattr(homelab, "_update_action", lambda s, item, payload, trigger: (started.append(payload["containers"]) or {"started": 1, "order": payload["containers"]}, "Updating 1 in the background"))
    now = datetime.now(UTC)
    siris.state.set("internet_log", {"outages": [{"start": (now - timedelta(days=2)).isoformat(), "end": (now - timedelta(days=2) + timedelta(minutes=3)).isoformat(), "seconds": 180}],
                                     "speeds": [{"at": (now - timedelta(days=1)).isoformat(), "down": 95.2, "up": 19.8, "ping": 12},
                                                {"at": (now - timedelta(days=40)).isoformat(), "down": 99, "up": 20, "ping": 10}]})
    _with(siris)
    try:
        r = client.get("/siris/hub/v1/containers", headers=H).json()
        assert r["available"] and [u["container"] for u in r["updates"]] == ["sonarr"] and r["running"] is False
        u = client.post("/siris/hub/v1/containers/update", json={"containers": ["sonarr", "not-there"]}, headers=H).json()
        assert u == {"started": 1, "order": ["sonarr"], "message": "Updating 1 in the background"} and started == [["sonarr"]]
        assert client.post("/siris/hub/v1/containers/update", json={"containers": ["nope"]}, headers=H).json()["started"] == 0
        i = client.get("/siris/hub/v1/internet", params={"days": 30}, headers=H).json()
        assert [s["down"] for s in i["speeds"]] == [95.2] and i["outages"][0]["seconds"] == 180 and i["minutes_down"] == 3.0
    finally:
        app.dependency_overrides.pop(get_siris, None)
