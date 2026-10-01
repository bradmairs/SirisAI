"""The car, through whatever Home Assistant integration it already has
(Tesla, Kia/Hyundai, BMW, Polestar, OBD dongles...): battery, range,
charging, where it is, locked or not -- and preconditioning the cabin.

Each piece is its own entity setting (SIRISAI_CAR_*_ENTITY_ID), all
optional; only what's configured is read.
"""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.core.config import Settings
    from app.home_assistant.client import HomeAssistantClient

_TRUTHY = {"on", "true", "charging", "plugged", "plugged_in", "connected", "yes"}


def _entities(settings: "Settings") -> dict[str, str]:
    return {
        key: value
        for key, value in {
            "battery": settings.car_battery_entity_id,
            "range": settings.car_range_entity_id,
            "charging": settings.car_charging_entity_id,
            "plugged_in": settings.car_plugged_entity_id,
            "location": settings.car_location_entity_id,
            "locked": settings.car_lock_entity_id,
            "climate": settings.car_climate_entity_id,
        }.items()
        if value
    }


def configured(settings: "Settings") -> bool:
    return bool(_entities(settings))


def status(client: "HomeAssistantClient", settings: "Settings") -> dict[str, Any]:
    out: dict[str, Any] = {"name": settings.car_name}
    for key, entity_id in _entities(settings).items():
        state = client.get_state_or_none(entity_id)
        if state is None:
            out[key] = None
            continue
        raw = str(state.get("state"))
        unit = (state.get("attributes") or {}).get("unit_of_measurement")
        if key in ("battery", "range"):
            try:
                out[key] = float(raw)
            except ValueError:
                out[key] = None
            if key == "range" and unit:
                out["range_unit"] = unit
        elif key in ("charging", "plugged_in"):
            out[key] = raw.lower() in _TRUTHY
        elif key == "locked":
            out[key] = raw.lower() == "locked"
        elif key == "location":
            out[key] = raw.replace("_", " ")
        else:
            out[key] = raw
    battery = out.get("battery")
    out["low_battery"] = battery is not None and battery < settings.car_low_battery_percent and not out.get("plugged_in")
    return out


def describe(car: dict[str, Any]) -> str:
    bits = []
    if car.get("battery") is not None:
        bits.append(f"{car['battery']:.0f}%")
    if car.get("range") is not None:
        bits.append(f"{car['range']:.0f} {car.get('range_unit') or 'km'} range")
    if car.get("charging"):
        bits.append("charging")
    elif car.get("plugged_in"):
        bits.append("plugged in")
    if car.get("location"):
        bits.append(f"at {car['location']}" if car["location"] not in ("home", "not home") else ("at home" if car["location"] == "home" else "away"))
    if car.get("locked") is False:
        bits.append("UNLOCKED")
    return f"{car['name'][0].upper()}{car['name'][1:]}: " + ", ".join(bits) if bits else f"No data for {car['name']}"


def precondition(client: "HomeAssistantClient", settings: "Settings", on: bool, temperature: float | None) -> dict[str, Any]:
    entity = settings.car_climate_entity_id
    if not entity:
        raise ValueError("No car climate entity configured (SIRISAI_CAR_CLIMATE_ENTITY_ID).")
    domain = entity.split(".", 1)[0]
    if domain == "climate":
        client.call_service("climate", "turn_on" if on else "turn_off", entity_id=entity)
        if on and temperature is not None:
            client.call_service("climate", "set_temperature", entity_id=entity, temperature=temperature)
    elif domain == "button":
        if not on:
            raise ValueError("That car's climate is a button -- it can only be started.")
        client.call_service("button", "press", entity_id=entity)
    else:
        client.call_service(domain, "turn_on" if on else "turn_off", entity_id=entity)
    return {"entity_id": entity, "on": on, "temperature": temperature}
