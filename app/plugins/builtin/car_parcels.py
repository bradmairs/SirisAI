"""Car (app/car/service.py) and parcel (app/parcels/service.py) tools."""

from typing import TYPE_CHECKING, Any

from app.car import service as car
from app.parcels import service as parcels
from app.plugins.base import Tool, ToolPermission

if TYPE_CHECKING:
    from app.core.siris import SirisAI


class CarStatusTool(Tool):
    name = "car_status"
    description = "The car's battery/fuel level, range, whether it's charging or plugged in, where it is, and whether it's locked."
    permission = ToolPermission.READ
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, siris: "SirisAI") -> None:
        self._siris = siris

    def run(self, **kwargs: Any) -> Any:
        assert self._siris._home_assistant_client is not None
        status = car.status(self._siris._home_assistant_client, self._siris.settings)
        return {**status, "summary": car.describe(status)}


class CarPreconditionTool(Tool):
    name = "car_precondition"
    description = "Start (or stop) warming/cooling the car's cabin before a trip, optionally to a temperature."
    permission = ToolPermission.SAFE_ACTION
    parameters = {
        "type": "object",
        "properties": {"on": {"type": "boolean"}, "temperature": {"type": "number", "description": "Target °C."}},
        "required": ["on"],
    }

    def __init__(self, siris: "SirisAI") -> None:
        self._siris = siris

    def run(self, on: bool = True, temperature: float | None = None, **kwargs: Any) -> Any:
        assert self._siris._home_assistant_client is not None
        return car.precondition(self._siris._home_assistant_client, self._siris.settings, bool(on), temperature)


class ParcelAddTool(Tool):
    name = "parcel_add"
    description = "Start tracking a delivery by its tracking number, with a short label like 'headphones'."
    permission = ToolPermission.SAFE_ACTION
    parameters = {
        "type": "object",
        "properties": {"tracking_number": {"type": "string"}, "label": {"type": "string"}, "carrier": {"type": "string"}},
        "required": ["tracking_number"],
    }

    def __init__(self, siris: "SirisAI") -> None:
        self._siris = siris

    def run(self, tracking_number: str = "", label: str = "", carrier: str = "", **kwargs: Any) -> Any:
        if not tracking_number.strip():
            raise ValueError("A tracking number is required.")
        parcel = self._siris.parcels.add(tracking_number, label, carrier)
        tracker = self._siris.parcel_tracker
        if tracker is not None:
            tracker.register(parcel["tracking_number"])
            parcels.refresh(self._siris.parcels, tracker)
            parcel = next(p for p in self._siris.parcels.active() if p["id"] == parcel["id"])
        return parcel


class ParcelListTool(Tool):
    name = "parcel_list"
    description = "Deliveries being tracked and where each one is (in transit, out for delivery, delivered...)."
    permission = ToolPermission.READ
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, siris: "SirisAI") -> None:
        self._siris = siris

    def run(self, **kwargs: Any) -> Any:
        return self._siris.parcels.active()


class ParcelDoneTool(Tool):
    name = "parcel_done"
    description = "Stop tracking a delivery (it arrived, or you don't care any more), by id from parcel_list."
    permission = ToolPermission.SAFE_ACTION
    parameters = {"type": "object", "properties": {"parcel_id": {"type": "integer"}}, "required": ["parcel_id"]}

    def __init__(self, siris: "SirisAI") -> None:
        self._siris = siris

    def run(self, parcel_id: int = 0, **kwargs: Any) -> Any:
        updated = self._siris.parcels.update(parcel_id, active=False)
        return {"parcel_id": parcel_id, "done": updated is not None}
