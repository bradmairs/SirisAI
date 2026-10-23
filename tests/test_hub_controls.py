"""Home controls for SirisOS 4.2: lights, garage doors, locks and scenes."""

import httpx

from app.core import guest
from app.hub import contract as c
from tests.test_hub import FakeHA, _guest_off, _wire_ha, full_siris, use  # noqa: F401 - fixtures


class HA(FakeHA):
    def __init__(self) -> None:
        super().__init__()
        self.states["light.kitchen"]["attributes"] = {"friendly_name": "Kitchen", "brightness": 128}
        self.states["light.deck"] = {"state": "off", "attributes": {"friendly_name": "Deck"}}
        self.states["cover.garage_door"]["attributes"]["friendly_name"] = "Garage door"
        self.states["cover.blinds"] = {"state": "open", "attributes": {"friendly_name": "Lounge blinds", "device_class": "blind"}}
        self.states["lock.front_door"]["attributes"] = {"friendly_name": "Front door"}
        self.states["scene.movie_night"] = {"state": "scening", "attributes": {"friendly_name": "Movie night"}}
        self.calls: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/api/services/"):
            self.calls.append(request.url.path)
            if request.url.path.endswith("/toggle"):
                entity = __import__("json").loads(request.content)["entity_id"]
                self.states[entity]["state"] = "off" if self.states[entity]["state"] == "on" else "on"
                return httpx.Response(200, json=[])
        return super().handler(request)


def _siris(ha: HA, **settings):
    siris = full_siris(home_assistant_base_url="http://ha", home_assistant_token="t", **settings)
    _wire_ha(siris, ha)
    return siris


def test_controls_list_garage_locks_lights_and_scenes(use) -> None:
    ha = HA()
    siris = _siris(ha)
    siris.scenes.create_scene("Wind down", [{"domain": "light", "service": "turn_off", "entity_id": "light.deck"}])
    body = c.Controls.model_validate(use(siris).get("/siris/hub/v1/controls").json())
    assert [(x.name, x.kind) for x in body.controls] == [("Garage door", "garage"), ("Front door", "lock"), ("Deck", "light"), ("Kitchen", "light")]
    assert body.controls[0].confirm == "Close Garage door?" and body.controls[1].confirm is None  # locking needs no yes
    assert body.controls[3].brightness_pct == 50
    assert [(s.name, s.source) for s in body.scenes] == [("Wind down", "siris"), ("Movie night", "home_assistant")]


def test_a_pinned_list_and_guest_mode(use) -> None:
    ha = HA()
    siris = _siris(ha, hub_controls="light.kitchen, cover.garage_door, sensor.nope")
    client = use(siris)
    assert [x["entity_id"] for x in client.get("/siris/hub/v1/controls").json()["controls"]] == ["light.kitchen", "cover.garage_door"]
    guest.set_mode(siris, True)
    body = client.get("/siris/hub/v1/controls").json()
    assert body["guest_mode"] is True and [x["entity_id"] for x in body["controls"]] == ["light.kitchen"]
    assert client.post("/siris/hub/v1/controls/cover.garage_door", json={"action": "close", "confirmed": True}).status_code == 404


def test_toggling_a_light_and_the_garage_needs_a_yes(use) -> None:
    ha = HA()
    siris = _siris(ha)
    client = use(siris)
    r = client.post("/siris/hub/v1/controls/light.deck", json={"action": "toggle"})
    assert r.json() == {"entity_id": "light.deck", "action": "toggle", "state": "on"}
    assert client.post("/siris/hub/v1/controls/cover.garage_door", json={"action": "close"}).status_code == 409
    assert ha.states["cover.garage_door"]["state"] == "open"
    assert client.post("/siris/hub/v1/controls/cover.garage_door", json={"action": "close", "confirmed": True}).json()["state"] == "closed"
    assert client.post("/siris/hub/v1/controls/lock.front_door", json={"action": "lock"}).json()["state"] == "locked"
    assert client.post("/siris/hub/v1/controls/lock.front_door", json={"action": "unlock"}).status_code == 409
    assert client.post("/siris/hub/v1/controls/light.deck", json={"action": "unlock", "confirmed": True}).status_code == 422
    assert client.post("/siris/hub/v1/controls/cover.blinds", json={"action": "close", "confirmed": True}).status_code == 404  # not a garage
    assert client.post("/siris/hub/v1/controls/light.nope", json={"action": "toggle"}).status_code == 404
    records = siris.audit.find_recent("home_assistant_call_service", "light.deck")
    assert any("tapped toggle on light.deck" in str(r.get("trigger") or "") for r in records)


def test_scenes_run(use) -> None:
    ha = HA()
    siris = _siris(ha)
    scene = siris.scenes.create_scene("Wind down", [{"domain": "light", "service": "turn_off", "entity_id": "light.kitchen"}])
    client = use(siris)
    assert client.post(f"/siris/hub/v1/scenes/siris:{scene['id']}/run").json()["action"] == "run"
    assert ha.states["light.kitchen"]["state"] == "off"
    assert client.post("/siris/hub/v1/scenes/scene.movie_night/run").status_code == 200
    assert "/api/services/scene/turn_on" in ha.calls
    assert client.post("/siris/hub/v1/scenes/siris:999/run").status_code == 404
    assert client.post("/siris/hub/v1/scenes/script.evil/run").status_code == 404


def test_without_home_assistant(use) -> None:
    body = use(full_siris()).get("/siris/hub/v1/controls").json()
    assert body["configured"] is False and body["controls"] == []
