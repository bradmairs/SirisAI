"""The household (app/household/): the weekly energy report, away mode,
and the camera digest."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core import daily_jobs
from app.core.config import Settings
from app.core.siris import SirisAI
from app.db.models import AttentionItem
from app.db.session import SessionLocal
from app.household import away, cameras, energy
from app.plugins.base import Tool, ToolPermission
from app.protocols import service as protocols

NOW = datetime(2026, 10, 26, 22, 0, tzinfo=UTC)  # Monday 9am in Melbourne


@pytest.fixture(autouse=True)
def _clean(client: TestClient):
    with SessionLocal() as session:
        session.query(AttentionItem).delete()
        session.commit()
    s = SirisAI(settings=Settings())
    for key in (away.STATE_KEY, protocols.LAST_RUN_KEY, cameras.STATE_KEY, protocols.SWITCHES_KEY):
        s.state.delete(key)
    for proto in s.protocols.list():
        s.protocols.delete(proto["name"])
    yield


def _items(siris: SirisAI, source: str) -> dict[str, dict[str, Any]]:
    return {i["title"]: i for i in siris.attention.list() if i["source"] == source}


class Recorder(Tool):
    permission = ToolPermission.SAFE_ACTION
    parameters = {"type": "object", "properties": {}, "required": []}
    description = "records calls"

    def __init__(self, name: str) -> None:
        self.name = name
        self.calls: list[dict[str, Any]] = []

    def run(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return {"ok": True}


# -- energy -------------------------------------------------------------------------------


def _meter(start: datetime, days: int, per_day: float, reset_on: int | None = None) -> list[tuple[datetime, str]]:
    """A counting-up kWh meter read every 6 hours."""
    points, value = [], 1000.0
    for i in range(days * 4 + 1):
        if reset_on is not None and i == reset_on:
            value = 0.0
        points.append((start + timedelta(hours=6 * i), f"{value:.3f}"))
        value += per_day / 4
    return points


class FakeHA:
    def __init__(self, states: list[dict[str, Any]], history: dict[str, list[tuple[datetime, str]]]) -> None:
        self.states, self.history = states, history

    def list_states(self, domain: str | None = None) -> list[dict[str, Any]]:
        return self.states

    def get_history_many(self, ids: list[str], start: datetime, end: datetime | None = None) -> dict[str, list[tuple[datetime, str]]]:
        # Like Home Assistant: each series starts with the state as of `start`.
        out = {}
        for k, v in self.history.items():
            if k in ids:
                before = [p for p in v if p[0] < start]
                out[k] = ([(start, before[-1][1])] if before else []) + [p for p in v if start <= p[0] <= (end or p[0])]
        return out


def _sensor(entity: str, name: str, kind: str, unit: str) -> dict[str, Any]:
    attrs = {"friendly_name": name, "device_class": kind, "unit_of_measurement": unit}
    if kind == "energy":
        attrs["state_class"] = "total_increasing"
    return {"entity_id": entity, "state": "0", "attributes": attrs}


def _power_nights(start: datetime, days: int, night_watts: float, day_watts: float, tz: str) -> list[tuple[datetime, str]]:
    from app.core import clock

    zone = clock.get_zone(tz)
    points = []
    t = start
    while t <= start + timedelta(days=days):
        hour = t.astimezone(zone).hour
        points.append((t, str(night_watts if hour < 5 else day_watts)))
        t += timedelta(hours=1)
    return points


def test_counting_up_meters_survive_resets() -> None:
    t0 = NOW - timedelta(days=1)
    pts = [(t0, 10.0), (t0 + timedelta(hours=1), 12.0), (t0 + timedelta(hours=2), 1.0), (t0 + timedelta(hours=3), 4.0)]
    assert sum(d for _, d in energy.deltas(pts, t0 - timedelta(hours=1), NOW)) == 2 + 1 + 3


def test_the_weekly_energy_report(monkeypatch: pytest.MonkeyPatch) -> None:
    start = NOW - timedelta(days=15)
    states = [
        _sensor("sensor.pool_pump_energy", "Pool pump", "energy", "kWh"),
        _sensor("sensor.heat_pump_energy", "Heat pump", "energy", "Wh"),
        _sensor("sensor.solar_production", "Solar production", "energy", "kWh"),
        _sensor("sensor.fridge_energy", "Fridge", "energy", "kWh"),
        _sensor("sensor.office_pc_power", "Office PC", "power", "W"),
        _sensor("sensor.tv_power", "TV", "power", "W"),
        _sensor("sensor.price", "Price", "monetary", "$/kWh"),
    ]
    history = {
        "sensor.pool_pump_energy": _meter(start, 15, 5.0),
        "sensor.heat_pump_energy": [(t, str(float(v) * 1000)) for t, v in _meter(start, 15, 3.0, reset_on=30)],
        "sensor.solar_production": _meter(start, 15, 20.0),
        "sensor.fridge_energy": _meter(start, 15, 1.0),
        "sensor.office_pc_power": _power_nights(start, 15, 85, 120, "Australia/Melbourne"),
        "sensor.tv_power": _power_nights(start, 15, 1, 90, "Australia/Melbourne"),
        "sensor.price": [(start, "0.30")],
    }
    siris = SirisAI(settings=Settings(timezone="Australia/Melbourne", energy_price_entity_id="sensor.price", energy_ignore="Fridge"))
    siris._home_assistant_client = FakeHA(states, history)  # type: ignore[assignment]
    r = energy.weekly(siris, NOW)
    assert [d["name"] for d in r["biggest"]] == ["Pool pump", "Heat pump"]
    assert r["kwh"] == pytest.approx(56, abs=1.5)  # 5 + 3 kWh a day, not the solar or the ignored fridge
    assert r["cost"] == pytest.approx(r["kwh"] * 0.30, abs=0.5)
    assert r["left_on_overnight"] == [{"entity_id": "sensor.office_pc_power", "name": "Office PC", "nights": 7, "watts": 85}]
    assert r["text"].startswith(f"Energy last week: {r['kwh']:g} kWh ($")
    assert "Left on overnight: Office PC (7 nights, about 85 W)." in r["text"]
    assert next(iter(_items(siris, "energy-weekly").values()))["severity"] == "attention"

    flat = SirisAI(settings=Settings(timezone="Australia/Melbourne", energy_flat_rate=0.25, energy_meter_entity_id="sensor.pool_pump_energy"))
    flat._home_assistant_client = FakeHA(states, history)  # type: ignore[assignment]
    r2 = energy.report(flat, 7, NOW)
    assert r2["kwh"] == pytest.approx(35, abs=1.5) and r2["cost"] == pytest.approx(r2["kwh"] * 0.25, abs=0.1)
    assert r2["change_percent"] == 0


# -- away mode ----------------------------------------------------------------------------


def _away_siris(mode: str = "ask") -> tuple[SirisAI, Recorder, Recorder]:
    siris = SirisAI(settings=Settings(away_mode=mode, away_protocol="leaving"))
    ha, notify = Recorder("home_assistant_call_service"), Recorder("notify")
    siris.tools.register(ha)
    siris.tools.register(notify)
    siris.protocols.save("leaving", "Out the door", [{"tool": "notify", "arguments": {"message": "bye"}, "label": "Say bye"}])
    return siris, ha, notify


def test_leaving_offers_the_protocol_and_running_it_works() -> None:
    from app.hub import attention

    siris, _, notify = _away_siris()
    result = away.on_change(siris, home=False)
    assert result["offered"] == "leaving" and result["steps"] == ["Say bye"]
    offer = _items(siris, "away")["Everyone's out. Run leaving?"]
    assert "Open SirisAI to run it" in notify.calls[0]["message"]
    closed, _ = attention.act(siris, offer["id"], "run")
    assert closed["outcome"] == "Ran leaving (1 step)" and notify.calls[-1] == {"message": "bye"}
    assert siris.state.get(away.STATE_KEY)["protocol"] == "leaving"


def test_coming_home_undoes_it_but_keeps_the_house_locked() -> None:
    siris, ha, _ = _away_siris("auto")
    away.on_change(siris, home=False)
    assert "Everyone's out, so I ran leaving" in _items(siris, "away")
    # Pretend the protocol changed lights, a lock and the garage.
    last = siris.state.get(protocols.LAST_RUN_KEY)
    last["undo"] = [
        {"tool": "home_assistant_call_service", "arguments": {"domain": "light", "service": "turn_on", "entity_id": "light.lounge"}},
        {"tool": "home_assistant_call_service", "arguments": {"domain": "lock", "service": "unlock", "entity_id": "lock.front"}},
        {"tool": "home_assistant_call_service", "arguments": {"domain": "cover", "service": "open_cover", "entity_id": "cover.garage"}},
    ]
    siris.state.set(protocols.LAST_RUN_KEY, last)
    result = away.on_change(siris, home=True)
    assert result["kept_secure"] == 2
    assert ha.calls == [{"domain": "light", "service": "turn_on", "entity_id": "light.lounge"}]
    assert not _items(siris, "away")


def test_coming_home_leaves_other_protocols_and_offers_alone() -> None:
    siris, ha, _ = _away_siris()
    away.on_change(siris, home=False)  # offered, never run
    assert away.on_change(siris, home=True) is None and not _items(siris, "away")
    assert ha.calls == []
    off = SirisAI(settings=Settings(away_mode="off"))
    assert away.on_change(off, home=False) is None


def test_a_protocol_turned_off_is_never_run_or_offered_and_one_with_notifications_off_stays_quiet() -> None:
    """5.11: per protocol, active (off: it never runs, by hand or by itself) and notify (off: nothing to the phone)."""
    siris, _, notify = _away_siris()
    protocols.set_switches(siris, "leaving", notify=False)
    result = away.on_change(siris, home=False)
    assert result["offered"] == "leaving" and notify.calls == []  # offered in the feed, not pushed
    assert _items(siris, "away")["Everyone's out. Run leaving?"]["severity"] == "info"
    run = protocols.run(siris, "leaving", "test")
    assert run["steps"][0]["status"] == "skipped" and "notifications are off" in run["steps"][0]["reason"] and notify.calls == []
    assert protocols.get_protocol(siris, "leaving")["notify"] is False

    away.on_change(siris, home=True)
    protocols.set_switches(siris, "leaving", active=False, notify=True)
    assert away.on_change(siris, home=False) == {"skipped": "'leaving' is turned off"} and not _items(siris, "away")
    with pytest.raises(protocols.Inactive):
        protocols.run(siris, "leaving", "test")
    assert protocols.switches(siris) == {"leaving": {"active": False, "notify": True}}
    protocols.set_switches(siris, "leaving", active=True)
    assert protocols.switches(siris) == {}  # back to the defaults: nothing kept


# -- camera digest ------------------------------------------------------------------------


class FakeFrigate:
    def __init__(self, events: list[dict[str, Any]]) -> None:
        self.events = events
        self.asked: list[datetime] = []

    def events_since(self, after: datetime, limit: int = 300) -> list[dict[str, Any]]:
        self.asked.append(after)
        return [e for e in self.events if datetime.fromisoformat(e["start_time"]) >= after]


def _event(camera: str, label: str, at: datetime, sub: str | None = None, description: str | None = None) -> dict[str, Any]:
    return {"id": at.isoformat(), "camera": camera, "label": label, "sub_label": sub, "start_time": at.isoformat(), "description": description}


def test_the_camera_digest_summarises_since_the_last_one() -> None:
    siris = SirisAI(settings=Settings(timezone="UTC", camera_digest_hours="12,18", camera_digest_push=True))
    notify = Recorder("notify")
    siris.tools.register(notify)
    day = datetime(2026, 10, 29, tzinfo=UTC)
    siris._frigate_client = FakeFrigate(  # type: ignore[assignment]
        [
            _event("front_door", "person", day.replace(hour=9, minute=30), sub="Brad"),
            _event("front_door", "amazon", day.replace(hour=11, minute=4)),
            _event("front_door", "package", day.replace(hour=11, minute=5), description="A box left by the door."),
            _event("driveway", "car", day.replace(hour=10)),
        ]
    )
    assert not cameras.due(siris, day.replace(hour=11))
    assert cameras.due(siris, day.replace(hour=12, minute=1))
    result = cameras.digest(siris, day.replace(hour=12, minute=1))
    assert result["text"] == "Front Door: 2 people (Brad at 09:30, a courier at 11:04), 1 package. Driveway: 1 car."
    item = next(iter(_items(siris, "cameras").values()))
    assert "Front Door 11:05: A box left by the door." in item["body"]
    assert notify.calls and notify.calls[0]["title"] == "Cameras"
    assert not cameras.due(siris, day.replace(hour=13))
    assert cameras.due(siris, day.replace(hour=18, minute=5))
    assert cameras.digest(siris, day.replace(hour=18, minute=5)) == {"events": 0}  # nothing new, no digest


def test_the_household_jobs_are_scheduled() -> None:
    names = {job.name: job for job in daily_jobs.JOBS}
    assert names["energy_weekly"].weekday(SirisAI(settings=Settings())) == 0


def test_home_assistant_history_for_several_entities_in_one_request() -> None:
    import httpx

    from app.home_assistant.client import HomeAssistantClient

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.url.params))
        # minimal_response: only the first entry of each series names the entity.
        return httpx.Response(200, json=[
            [{"entity_id": "sensor.a", "state": "1.5", "last_changed": "2026-10-26T00:00:00+00:00"}, {"state": "2.0", "last_changed": "2026-10-26T06:00:00Z"}],
            [{"entity_id": "sensor.b", "state": "unavailable", "last_updated": "2026-10-26T00:00:00+00:00"}],
        ])

    client = HomeAssistantClient("http://ha", "t", transport=httpx.MockTransport(handler))
    out = client.get_history_many(["sensor.a", "sensor.b"], NOW - timedelta(days=1), NOW)
    assert seen["filter_entity_id"] == "sensor.a,sensor.b" and "minimal_response" in seen
    assert [v for _, v in out["sensor.a"]] == ["1.5", "2.0"] and out["sensor.a"][1][0].tzinfo is not None
    assert out["sensor.b"][0][1] == "unavailable"
