"""Car via Home Assistant (app/car/service.py) and parcel tracking
(app/parcels/service.py)."""

import json
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.scheduler import run_parcel_refresh
from app.core.siris import SirisAI
from app.parcels.service import SeventeenTrackClient
from app.skills.builtin.briefing_extras import briefing_lines
from tests.test_protocols import FakeHA, _siris


def _car_siris(ha: FakeHA, **settings: Any) -> SirisAI:
    ha.states.update({
        "sensor.ev_battery": {"state": "24", "attributes": {"unit_of_measurement": "%"}},
        "sensor.ev_range": {"state": "96", "attributes": {"unit_of_measurement": "km"}},
        "binary_sensor.ev_plugged": {"state": "off", "attributes": {}},
        "device_tracker.ev": {"state": "home", "attributes": {}},
        "lock.ev": {"state": "unlocked", "attributes": {}},
        "climate.ev": {"state": "off", "attributes": {}},
    })
    return _siris(ha, car_name="the Polestar", car_battery_entity_id="sensor.ev_battery", car_range_entity_id="sensor.ev_range",
                  car_plugged_entity_id="binary_sensor.ev_plugged", car_location_entity_id="device_tracker.ev",
                  car_lock_entity_id="lock.ev", car_climate_entity_id="climate.ev", **settings)


def test_car_status_and_briefing_warning(client: TestClient) -> None:
    ha = FakeHA()
    siris = _car_siris(ha)
    status = siris.run_tool("car_status", {})
    assert status["battery"] == 24 and status["range"] == 96 and status["plugged_in"] is False
    assert status["low_battery"] is True and status["locked"] is False
    assert status["summary"] == "The Polestar: 24%, 96 km range, at home, UNLOCKED"
    assert "The Polestar: 24%, 96 km range, at home, UNLOCKED. It's low and not plugged in -- charge it tonight." in briefing_lines(siris)
    departure = siris.tools.get("departure_check").run()  # type: ignore[union-attr]
    assert departure["car"] == "The Polestar is at 24% and not plugged in"


def test_car_range_in_miles_is_shown_in_km(client: TestClient) -> None:
    """SI only (4.05)."""
    ha = FakeHA()
    siris = _car_siris(ha)
    ha.states["sensor.ev_range"] = {"state": "60", "attributes": {"unit_of_measurement": "mi"}}
    status = siris.run_tool("car_status", {})
    assert status["range"] == 96.6 and status["range_unit"] == "km" and "97 km range" in status["summary"]


def test_car_precondition_calls_the_climate_entity(client: TestClient) -> None:
    ha = FakeHA()
    siris = _car_siris(ha)
    original = ha.handler

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/services/climate/set_temperature":
            ha.calls.append(("climate", "set_temperature", json.loads(request.content)))
            return httpx.Response(200, json=[])
        return original(request)

    siris._home_assistant_client._client._transport = httpx.MockTransport(handler)  # type: ignore[union-attr]
    siris.run_tool("car_precondition", {"on": True, "temperature": 21})
    assert ("climate", "turn_on", "climate.ev") in ha.calls
    assert ha.calls[-1] == ("climate", "set_temperature", {"entity_id": "climate.ev", "temperature": 21})


def _tracker(statuses: dict[str, str], registered: list[str]) -> SeventeenTrackClient:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["17token"] == "key"
        body = json.loads(request.content)
        if request.url.path == "/track/v2.2/register":
            registered.extend(b["number"] for b in body)
            return httpx.Response(200, json={"code": 0, "data": {"accepted": body}})
        return httpx.Response(200, json={"code": 0, "data": {"accepted": [
            {"number": b["number"], "carrier": 3011, "track_info": {
                "latest_status": {"status": statuses.get(b["number"], "InTransit")},
                "latest_event": {"time_iso": "2026-10-01T08:12:00+10:00", "description": "With courier for delivery", "location": "Melbourne"},
                "time_metrics": {"estimated_delivery_date": {"from": "2026-10-01T00:00:00+10:00"}},
            }} for b in body]}})

    return SeventeenTrackClient("key", transport=httpx.MockTransport(handler))


@pytest.fixture
def parcel_siris(client: TestClient) -> Any:
    siris = SirisAI(settings=Settings(ntfy_topic="t"))
    yield siris
    for parcel in siris.parcels.active():
        siris.parcels.update(parcel["id"], active=False)


def test_parcels_track_refresh_and_notify_on_change(parcel_siris: SirisAI) -> None:
    siris = parcel_siris
    statuses: dict[str, str] = {}
    registered: list[str] = []
    siris.parcel_tracker = _tracker(statuses, registered)
    sent: list[dict[str, Any]] = []

    class Pushes:
        def send(self, message: str, title: str | None = None, actions: Any = None, priority: str | None = None) -> None:
            sent.append({"message": message, "priority": priority})

        def close(self) -> None:
            pass

    siris._notify_client = Pushes()  # type: ignore[assignment]
    parcel = siris.run_tool("parcel_add", {"tracking_number": "LX 123 456 789 AU", "label": "headphones"})
    assert registered == ["LX123456789AU"]
    assert parcel["status"] == "InTransit" and parcel["status_text"] == "in transit"
    assert run_parcel_refresh(siris) == []  # no change, no push

    statuses["LX123456789AU"] = "OutForDelivery"
    changed = run_parcel_refresh(siris)
    assert [p["label"] for p in changed] == ["headphones"]
    assert sent == [{"message": "headphones is out for delivery. 2026-10-01T08:12:00+10:00 -- With courier for delivery -- Melbourne", "priority": "high"}]
    assert "Parcels: headphones out for delivery (ETA 2026-10-01)." in briefing_lines(siris)

    assert siris.run_tool("parcel_done", {"parcel_id": parcel["id"]}) == {"parcel_id": parcel["id"], "done": True}
    assert all(p["id"] != parcel["id"] for p in siris.run_tool("parcel_list", {}))


def test_parcels_without_a_tracker_are_a_manual_list(parcel_siris: SirisAI) -> None:
    parcel = parcel_siris.run_tool("parcel_add", {"tracking_number": "ABC123", "label": "book"})
    assert parcel["status_text"] == "status unknown"
    assert run_parcel_refresh(parcel_siris) == []
