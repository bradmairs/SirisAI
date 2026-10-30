"""/siris/hub/v1/energy/usage (3.11): Home Assistant's power sensors and meters."""

from app.hub import compose
from tests.test_hub import full_siris


class FakeHA:
    def list_states(self, domain=None):
        return [
            {"entity_id": "sensor.house_power", "state": "1.25", "attributes": {"device_class": "power", "unit_of_measurement": "kW", "friendly_name": "House power"}},
            {"entity_id": "sensor.dishwasher_power", "state": "1800", "attributes": {"device_class": "power", "unit_of_measurement": "W", "friendly_name": "Dishwasher power"}},
            {"entity_id": "sensor.office_plug_energy", "state": "1520", "attributes": {"device_class": "energy", "state_class": "total_increasing", "unit_of_measurement": "Wh", "friendly_name": "Office plug energy"}},
            {"entity_id": "sensor.broken", "state": "unavailable", "attributes": {"device_class": "power", "unit_of_measurement": "W"}},
            {"entity_id": "sensor.temp", "state": "21", "attributes": {"device_class": "temperature", "unit_of_measurement": "°C"}},
        ]


def test_lists_power_and_meters_with_the_house_figure():
    siris = full_siris(energy_grid_entity_id="sensor.house_power")
    assert compose.energy_usage(siris).configured in (True, False)
    siris._home_assistant_client = FakeHA()
    u = compose.energy_usage(siris)
    assert u.configured and u.house_watts == 1250
    assert [(p.name, p.watts) for p in u.power] == [("Dishwasher power", 1800), ("House power", 1250)]
    assert [(m.entity_id, m.kwh) for m in u.meters] == [("sensor.office_plug_energy", 1.52)]
