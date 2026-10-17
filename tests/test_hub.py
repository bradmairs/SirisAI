"""The hub contract (app/hub/, /siris/hub/v1): info, brief, widgets, search,
protocols, cameras and the LLM endpoint. The attention feed has its own file
(test_attention.py)."""

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.core import guest
from app.core.config import Settings
from app.core.siris import SirisAI, get_siris
from app.home_assistant.client import HomeAssistantClient
from app.hub import contract as c
from app.llm.base import LLMClient, LLMResponse
from app.main import app
from app.plugins.base import Tool, ToolPermission
from app.protocols import service as protocols

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "contracts" / "hub-v1" / "examples"


class FakeTool(Tool):
    permission = ToolPermission.READ
    parameters = {"type": "object", "properties": {}, "required": []}
    description = "fake"

    def __init__(self, name: str, result: Any) -> None:
        self.name = name
        self._result = result
        self.calls: list[dict[str, Any]] = []

    def run(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


def _fake(siris: SirisAI, name: str, result: Any) -> FakeTool:
    tool = FakeTool(name, result)
    siris.tools._tools[name] = tool
    return tool


class FakeHA:
    def __init__(self) -> None:
        self.states = {
            "lock.front_door": {"state": "unlocked", "attributes": {}},
            "cover.garage_door": {"state": "open", "attributes": {"device_class": "garage"}},
            "light.kitchen": {"state": "on", "attributes": {}},
        }

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/states":
            return httpx.Response(200, json=[{"entity_id": k, **v} for k, v in self.states.items()])
        if path.startswith("/api/states/"):
            entity = path.rsplit("/", 1)[-1]
            return httpx.Response(200, json={"entity_id": entity, **self.states[entity]}) if entity in self.states else httpx.Response(404)
        if path.startswith("/api/services/"):
            _, _, _, domain, svc = path.split("/")
            body = json.loads(request.content or b"{}")
            new = {"turn_on": "on", "turn_off": "off", "lock": "locked", "unlock": "unlocked", "open_cover": "open", "close_cover": "closed"}[svc]
            entity = body.get("entity_id")
            targets = [e for e in self.states if e.startswith(domain + ".")] if entity == "all" else entity if isinstance(entity, list) else [entity]
            for target in targets:
                if target in self.states:
                    self.states[target]["state"] = new
            return httpx.Response(200, json=[])
        return httpx.Response(404)


class FakeCalendar:
    """A CalendarSet for date-range reads."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.asked: list[tuple[Any, Any]] = []

    def events_between(self, start: Any, end: Any) -> list[dict[str, Any]]:
        self.asked.append((start, end))
        if self.error:
            raise self.error
        return [
            {"summary": "Bin night", "start": "2026-10-12", "end": "2026-10-13", "location": None, "calendar": "Home"},
            {"summary": "Stormwater webinar (IPWEA)", "start": "2026-10-14T12:00:00+11:00", "end": "2026-10-14T13:30:00+11:00", "location": "Online", "calendar": "Work"},
        ]


def full_siris(**settings: Any) -> SirisAI:
    """A SirisAI with every brief source faked, deterministic enough to
    produce the contract examples."""
    siris = SirisAI(settings=Settings(timezone="UTC", energy_cheap_windows="00:00-06:00", **settings))
    _fake(siris, "weather_current", {"temperature_c": 17.4, "humidity_percent": 61, "wind_speed_kmh": 11, "precipitation_mm": 0, "conditions": "Partly cloudy"})
    _fake(siris, "weather_forecast", [{"date": "2026-10-09", "weekday": "Friday", "conditions": "Slight rain", "high_c": 19, "low_c": 9, "rain_chance_percent": 70, "rain_mm": 4.2}])
    _fake(siris, "calendar_upcoming_events", [
        {"summary": "Site visit, Werribee", "start": "2026-10-09T10:30:00+11:00", "end": "2026-10-09T12:00:00+11:00", "location": "Werribee", "calendar": "Work"},
        {"summary": "Bin night", "start": "2026-10-09", "end": "2026-10-10", "location": None, "calendar": "Home"},
    ])
    _fake(siris, "home_assistant_get_todo_items", [{"list": "todo.shopping", "items": [{"summary": "Milk", "status": "needs_action", "due": None}]}])
    _fake(siris, "email_list_unread", [
        {"uid": "1", "from": "Dana Lee <dana@example.com>", "subject": "Drainage report comments", "important": True, "why": ["from a frequent contact"]},
        {"uid": "2", "from": "news@example.com", "subject": "Weekly digest", "important": False, "why": []},
    ])
    _fake(siris, "health_summary", {"metrics": {"sleep_hours": {"latest": 6.1, "day": "2026-10-09", "week_average": 7.0, "baseline": 7.3}}, "notes": ["slept 6.1h -- 1.2h under your usual"]})
    _fake(siris, "car_status", {"name": "the car", "battery": 24.0, "range": 96.0, "range_unit": "km", "charging": False, "plugged_in": False, "locked": True, "location": "home", "low_battery": True, "summary": "24%, 96 km range, locked, at home"})
    return siris


@pytest.fixture
def use(client: TestClient):
    def _use(siris: SirisAI) -> TestClient:
        app.dependency_overrides[get_siris] = lambda: siris
        return client

    yield _use
    app.dependency_overrides.pop(get_siris, None)


@pytest.fixture(autouse=True)
def _guest_off(client: TestClient):
    siris = SirisAI(settings=Settings())
    siris.state.delete(guest.STATE_KEY)
    siris.state.delete(protocols.LAST_RUN_KEY)
    yield
    siris.state.delete(guest.STATE_KEY)


def test_schema_file_matches_the_models() -> None:
    assert (ROOT / "contracts" / "hub-v1" / "schema.json").read_text() == c.schema_text(), "run: python -m app.hub.contract"


@pytest.mark.parametrize("name", sorted(p.stem for p in EXAMPLES.glob("*.json")))
def test_every_example_parses_as_its_model(name: str) -> None:
    payload = json.loads((EXAMPLES / f"{name}.json").read_text())
    c.ENDPOINTS[name].model_validate(payload)


def test_every_response_endpoint_has_an_example() -> None:
    responses = {k for k in c.ENDPOINTS if not k.endswith("_request")}
    assert responses <= {p.stem for p in EXAMPLES.glob("*.json")}


def test_info_reports_user_guest_and_features(use) -> None:
    siris = full_siris()
    client = use(siris)
    body = client.get("/siris/hub/v1", headers={"X-Siris-User": "brad"}).json()
    assert body["contract"] == "1" and body["user"] == "brad" and body["guest_mode"] is False
    assert body["features"]["weather"] and body["features"]["car"] and body["features"]["energy"]
    assert not body["features"]["sirisos"] and not body["features"]["cameras"]
    assert "health" in body["personal_sections"]


def test_brief_normalises_every_source(use) -> None:
    siris = full_siris()
    body = use(siris).get("/siris/hub/v1/brief").json()
    c.Brief.model_validate(body)
    assert body["weather"]["now"]["temperature_c"] == 17.4 and body["weather"]["now"]["wind_kmh"] == 11
    assert body["weather"]["today"] == {"date": "2026-10-09", "conditions": "Slight rain", "high_c": 19.0, "low_c": 9.0, "rain_chance_percent": 70.0, "precipitation_mm": 4.2}
    assert [e["title"] for e in body["calendar"]] == ["Bin night", "Site visit, Werribee"]  # all-day first
    assert [e["calendar"] for e in body["calendar"]] == ["Home", "Work"]  # which calendar, for the brief
    assert body["calendar"][0]["all_day"] is True
    assert body["todo"] == [{"summary": "Milk", "list": "todo.shopping", "due": None}]
    assert body["email"] == {"unread": 2, "important": [{"sender": "Dana Lee", "subject": "Drainage report comments", "why": ["from a frequent contact"]}]}
    assert body["health"]["metrics"]["sleep_hours"]["baseline"] == 7.3
    assert body["car"]["low_battery"] is True and body["car"]["battery_percent"] == 24.0
    assert body["energy"]["configured"] is True
    assert body["brain"] is None and body["parcels"] is None  # not configured: absent, not "unavailable"
    assert body["unavailable"] == [] and body["hidden"] == []


def test_a_failing_source_blanks_only_its_section(use) -> None:
    siris = full_siris()
    _fake(siris, "email_list_unread", RuntimeError("IMAP down"))
    body = use(siris).get("/siris/hub/v1/brief").json()
    assert body["email"] is None and body["unavailable"] == ["email"]
    assert body["weather"]["now"] is not None


def test_guest_mode_leaves_personal_sections_out(use) -> None:
    siris = full_siris()
    guest.set_mode(siris, True)
    client = use(siris)
    body = client.get("/siris/hub/v1/brief").json()
    assert body["guest_mode"] is True and "email" in body["hidden"]
    assert body["email"] is None and body["calendar"] is None and body["car"] is None
    assert body["weather"]["now"] is not None  # not personal
    assert siris.tools.get("email_list_unread").calls == []  # never even read
    assert client.get("/siris/hub/v1/search", params={"q": "drainage"}).json()["hits"] == []
    assert client.get("/siris/hub/v1").json()["guest_mode"] is True


def test_widgets(use) -> None:
    ha = FakeHA()
    siris = full_siris(home_assistant_base_url="http://ha", home_assistant_token="t")
    _wire_ha(siris, ha)
    body = use(siris).get("/siris/hub/v1/widgets").json()
    c.Widgets.model_validate(body)
    assert {"lockdown", "away"} <= {p["name"] for p in body["protocols"]}
    assert body["car"]["name"] == "the car" and body["cameras"] == []
    assert body["autonomy"]["summary"].startswith("I") and body["last_protocol"] is None


def _wire_ha(siris: SirisAI, ha: FakeHA) -> None:
    client = HomeAssistantClient(base_url="http://ha", token="t", transport=httpx.MockTransport(ha.handler))
    siris._home_assistant_client = client
    for tool in siris.tools.list():
        if isinstance(getattr(tool, "_client", None), HomeAssistantClient):
            tool._client = client  # type: ignore[attr-defined]


def test_protocol_preview_run_needs_confirmation_then_undo(use) -> None:
    ha = FakeHA()
    siris = SirisAI(settings=Settings(home_assistant_base_url="http://ha", home_assistant_token="t", timezone="UTC"))
    _wire_ha(siris, ha)
    client = use(siris)

    preview = client.get("/siris/hub/v1/protocols/lockdown").json()
    assert preview["name"] == "lockdown" and any(s["would_run"] for s in preview["steps"])
    held = client.post("/siris/hub/v1/protocols/lockdown/run", json={})
    assert held.status_code == 409 and ha.states["lock.front_door"]["state"] == "unlocked"
    assert held.json()["detail"]["preview"]["name"] == "lockdown"

    ran = client.post("/siris/hub/v1/protocols/lockdown/run", json={"confirmed": True}, headers={"X-Siris-Client": "SirisOS"})
    assert ran.status_code == 200, ran.text
    assert ha.states["lock.front_door"]["state"] == "locked" and ran.json()["can_undo"] is True
    audit = siris.audit.list_recent(limit=20)
    assert any(a["tool_name"] == "run_protocol" and "in SirisOS" in (a["trigger"] or "") for a in audit)

    assert client.post("/siris/hub/v1/protocols/undo", json={}).status_code == 409
    undone = client.post("/siris/hub/v1/protocols/undo", json={"confirmed": True})
    assert undone.status_code == 200 and ha.states["lock.front_door"]["state"] == "unlocked"
    assert client.post("/siris/hub/v1/protocols/undo", json={"confirmed": True}).status_code == 409  # nothing left
    assert client.get("/siris/hub/v1/protocols/nope").status_code == 404


def test_camera_look(use) -> None:
    siris = SirisAI(settings=Settings())
    client = use(siris)
    assert client.post("/siris/hub/v1/cameras/front_door/look", json={}).status_code == 404
    look = _fake(siris, "frigate_look_at_camera", {"camera": "front_door", "question": "Who's there?", "description": "A courier holding a parcel."})
    body = client.post("/siris/hub/v1/cameras/front_door/look", json={"question": "Who's there?"}).json()
    assert body == {"camera": "front_door", "description": "A courier holding a parcel.", "question": "Who's there?"}
    assert look.calls == [{"camera": "front_door", "question": "Who's there?"}]


class FakeLLM(LLMClient):
    model = "fake-model"

    def __init__(self) -> None:
        self.seen: list[list[dict[str, Any]]] = []

    async def generate(self, messages: list[dict[str, Any]], *, tools: Any = None, **kwargs: object) -> LLMResponse:
        self.seen.append(messages)
        return LLMResponse(text="Per [1], the minimum grade is 1 in 100.")


def test_llm_complete_goes_through_the_router_and_is_audited(use) -> None:
    llm = FakeLLM()
    siris = SirisAI(settings=Settings(), llm=llm)
    client = use(siris)
    body = client.post("/siris/hub/v1/llm/complete", json={"task": "sirishydro", "system": "Cite evidence.", "prompt": "Evidence [1]..."}).json()
    assert body["text"].startswith("Per [1]") and body["role"] == "general"
    assert llm.seen[-1] == [{"role": "system", "content": "Cite evidence."}, {"role": "user", "content": "Evidence [1]..."}]
    entry = siris.audit.list_recent(limit=1)[0]
    assert entry["tool_name"] == "hub_llm_complete" and entry["trigger"] == "hub: sirishydro asked for a completion"
    assert client.post("/siris/hub/v1/llm/complete", json={"task": "x", "prompt": ""}).status_code == 422


def test_search_finds_chats_and_memories(use) -> None:
    import uuid

    marker = uuid.uuid4().hex[:10]
    siris = SirisAI(settings=Settings())
    siris.memory.remember(f"Brad prefers Hazen-Williams {marker} for water mains")
    conv = siris.conversation.create_conversation()
    siris.conversation.add_message(conv, "user", f"what about {marker} pipes")
    body = use(siris).get("/siris/hub/v1/search", params={"q": marker}).json()
    c.SearchResults.model_validate(body)
    kinds = {h["kind"] for h in body["hits"]}
    assert kinds == {"memory", "chat"}
    chat = next(h for h in body["hits"] if h["kind"] == "chat")
    assert chat["ref"] == conv and chat["snippet"] == "You said"
    assert body["unavailable"] == []


def test_hud_summary_leaves_personal_tiles_out_in_guest_mode(use) -> None:
    siris = full_siris()
    siris.tools._tools["calendar_upcoming_events"].calls.clear()
    client = use(siris)
    assert client.get("/siris/hud/summary").json()["car"]["name"] == "the car"
    guest.set_mode(siris, True)
    calls_before = len(siris.tools.get("calendar_upcoming_events").calls)
    body = client.get("/siris/hud/summary").json()
    assert body["next_events"] is None and body["car"] is None and body["parcels"] is None and body["brain"] is None
    assert body["weather"]["temperature_c"] == 17.4 and body["guest_mode"]["on"] is True
    assert len(siris.tools.get("calendar_upcoming_events").calls) == calls_before  # never even read


def test_calendar_range_reads_every_event_in_local_dates(use) -> None:
    siris = full_siris()
    siris.settings = siris.settings.model_copy(update={"timezone": "Australia/Melbourne"})
    siris.calendar = FakeCalendar()  # type: ignore[assignment]
    client = use(siris)
    body = client.get("/siris/hub/v1/calendar", params={"start": "2026-10-12", "end": "2026-10-19"}).json()
    assert body["start"] == "2026-10-12" and body["end"] == "2026-10-19" and body["unavailable"] is False
    assert [e["title"] for e in body["events"]] == ["Bin night", "Stormwater webinar (IPWEA)"]
    assert body["events"][0]["all_day"] is True and body["events"][1]["calendar"] == "Work"
    start, end = siris.calendar.asked[0]
    assert start.isoformat() == "2026-10-12T00:00:00+11:00" and end.isoformat() == "2026-10-19T00:00:00+11:00"
    assert client.get("/siris/hub/v1/calendar", params={"start": "2026-10-12", "end": "2026-10-12"}).status_code == 422
    assert client.get("/siris/hub/v1/calendar", params={"start": "2026-01-01", "end": "2026-06-01"}).status_code == 422


def test_calendar_range_is_hidden_in_guest_mode_and_says_when_unavailable(use) -> None:
    siris = full_siris()
    client = use(siris)
    assert client.get("/siris/hub/v1/calendar", params={"start": "2026-10-12", "end": "2026-10-13"}).json()["unavailable"] is True
    siris.calendar = FakeCalendar(RuntimeError("iCloud down"))  # type: ignore[assignment]
    body = client.get("/siris/hub/v1/calendar", params={"start": "2026-10-12", "end": "2026-10-13"}).json()
    assert body["unavailable"] is True and body["events"] is None
    siris.calendar = FakeCalendar()  # type: ignore[assignment]
    guest.set_mode(siris, True)
    body = client.get("/siris/hub/v1/calendar", params={"start": "2026-10-12", "end": "2026-10-13"}).json()
    assert body["guest_mode"] is True and body["events"] is None and body["unavailable"] is False
