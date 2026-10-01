"""Energy-aware scheduling (app/energy/service.py)."""

from datetime import UTC, datetime, time, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.scheduler import tick
from app.core.siris import SirisAI
from app.energy import service
from app.plugins.base import ConfirmationRequired
from tests.test_permissions import _ConfirmRequiredTool
from tests.test_protocols import FakeHA, _siris


def test_parse_windows_and_midnight_crossing() -> None:
    windows = service.parse_windows("23:00-06:00, 11:00-14:00")
    assert windows == [(time(23), time(6)), (time(11), time(14))]
    assert service._in_window(time(2), windows[0]) and service._in_window(time(23, 30), windows[0])
    assert not service._in_window(time(7), windows[0])


def test_status_from_window_solar_and_price(client: TestClient) -> None:
    ha = FakeHA()
    ha.states["sensor.solar"] = {"state": "2.4", "attributes": {"unit_of_measurement": "kW"}}
    ha.states["sensor.price"] = {"state": "0.31", "attributes": {}}
    siris = _siris(ha, timezone="UTC", energy_cheap_windows="00:00-06:00", energy_solar_entity_id="sensor.solar", energy_price_entity_id="sensor.price", energy_price_max=0.2)

    night = service.status(siris, datetime(2026, 10, 1, 3, 0, tzinfo=UTC))
    assert night["cheap_now"] and "inside the cheap window 00:00-06:00" in night["reasons"]
    midday = service.status(siris, datetime(2026, 10, 1, 12, 0, tzinfo=UTC))
    assert midday["reasons"] == ["solar is producing 2400 W"]
    ha.states["sensor.solar"]["state"] = "0.3"
    evening = service.status(siris, datetime(2026, 10, 1, 19, 0, tzinfo=UTC))
    assert evening["cheap_now"] is False
    assert evening["next_cheap_window"] == "2026-10-02T00:00:00+00:00"
    ha.states["sensor.price"]["state"] = "0.12"
    assert service.status(siris, datetime(2026, 10, 1, 19, 0, tzinfo=UTC))["reasons"] == ["the price is 0.12 (at or under 0.2)"]


@pytest.fixture
def energy_siris(client: TestClient) -> SirisAI:
    siris = SirisAI(settings=Settings(timezone="UTC", energy_cheap_windows="01:00-05:00"))
    for job in siris.deferred_jobs.list("waiting"):
        siris.deferred_jobs.cancel(job["id"])
    if siris.tools.get("dangerous_action") is None:
        siris.tools.register(_ConfirmRequiredTool())
    return siris


def test_deferring_a_confirm_required_tool_needs_its_confirmation_and_carries_it(energy_siris: SirisAI) -> None:
    siris = energy_siris
    call = {"tool_name": "dangerous_action", "arguments": {"target": "dishwasher"}, "reason": "dishwasher"}
    with pytest.raises(ConfirmationRequired):
        siris.run_tool("schedule_for_cheap_energy", call)
    job = siris.run_tool("schedule_for_cheap_energy", call, confirmed=True)
    assert job["status"] == "waiting" and job["confirmed"] is True

    assert service.run_due(siris, datetime(2026, 10, 1, 18, 0, tzinfo=UTC)) == []  # expensive: wait
    ran = service.run_due(siris, datetime(2026, 10, 1, 2, 0, tzinfo=UTC))
    assert ran[0]["status"] == "done" and "cheap window" in ran[0]["why"]
    done = [j for j in siris.deferred_jobs.list(None) if j["id"] == job["id"]][0]
    assert done["status"] == "done" and '"acted_on": "dishwasher"' in done["outcome"]
    assert any("Ran dishwasher while power was cheap" in n["message"] for n in siris.pending_notifications.list_pending())


def test_deferring_a_safe_tool_needs_no_confirmation_and_deadline_forces_it(energy_siris: SirisAI) -> None:
    siris = energy_siris
    job = siris.run_tool("schedule_for_cheap_energy", {"tool_name": "echo", "arguments": {"message": "hi"}, "deadline_hours": 1})
    assert job["confirmed"] is False
    after_deadline = datetime.fromisoformat(job["deadline"]) + timedelta(minutes=5)
    if 1 <= after_deadline.hour < 5:  # make sure it's outside the cheap window
        after_deadline += timedelta(hours=6)
    ran = service.run_due(siris, after_deadline)
    assert ran[0]["why"] == "deadline reached"


def test_cancel_and_guards(energy_siris: SirisAI) -> None:
    siris = energy_siris
    job = siris.run_tool("schedule_for_cheap_energy", {"tool_name": "echo", "arguments": {"message": "x"}})
    assert siris.run_tool("cancel_deferred_job", {"job_id": job["id"]}) == {"job_id": job["id"], "cancelled": True}
    with pytest.raises(ValueError):
        siris.tools.get("schedule_for_cheap_energy").run(tool_name="schedule_for_cheap_energy")  # type: ignore[union-attr]
    unconfigured = SirisAI(settings=Settings())
    with pytest.raises(ValueError, match="No cheap-power signal"):
        unconfigured.tools.get("schedule_for_cheap_energy").run(tool_name="echo")  # type: ignore[union-attr]


def test_scheduler_tick_runs_due_jobs(energy_siris: SirisAI) -> None:
    siris = energy_siris
    siris.run_tool("schedule_for_cheap_energy", {"tool_name": "echo", "arguments": {"message": "tick"}})
    result = tick(siris, datetime(2026, 10, 1, 3, 0, tzinfo=UTC))
    assert result["deferred_jobs"] and result["deferred_jobs"][0]["status"] == "done"
