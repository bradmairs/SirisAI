"""Writes contracts/hub-v1/examples/ from real responses of a faked SirisAI.

    HUB_WRITE_EXAMPLES=1 pytest tests/test_hub_examples.py

Without the variable it checks instead that every committed example still
matches what the endpoints produce in shape (same keys at every level), so
an example can't quietly go stale when a composer changes. SirisOS's tests
parse these same files."""

import json
import os
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.siris import SirisAI, get_siris
from app.db.models import AttentionItem
from app.db.session import SessionLocal
from app.hub import contract as c
from app.main import app
from app.notify.confirmation import notify_confirmation_needed
from app.plugins.base import ToolPermission
from tests.test_attention import Recorder
from tests.test_hub import EXAMPLES, FakeCalendar, FakeHA, FakeLLM, _fake, _wire_ha, full_siris

WRITE = os.environ.get("HUB_WRITE_EXAMPLES") == "1"


class ExampleBrain:
    def insights(self, days: int = 30) -> dict[str, Any]:
        return {
            "summary": {"inbox": 2},
            "overdue_tasks": [{"task": "Send Werribee levels to Dana", "note": "Werribee Drainage", "days_late": 2}],
            "deadlines": [{"title": "Werribee Drainage", "days_left": 3}],
            "auto_linked": [{"a": "Hazen-Williams", "b": "Water Mains"}],
            "pending_auto_links": 1,
            "suggested_links": [{"a": "Detention Basins", "b": "Rational Method"}],
            "highlights": ["You added 6 notes this week, most about stormwater."],
            "topics": [{"tag": "stormwater", "count": 6}, {"tag": "ai", "count": 3}],
        }

    def search(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        return [{"title": "Hazen-Williams", "path": "Concepts/Hazen-Williams.md", "type": "concept", "tags": ["hydraulics"],
                 "excerpts": ["Friction headloss for water mains; C of 140 for new PVC."]}]


def _shape(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _shape(v) for k, v in sorted(value.items())}
    if isinstance(value, list):
        return [_shape(value[0])] if value else []
    return type(value).__name__ if value is not None else None


def _keys(value: Any) -> Any:
    """Every key path present, ignoring values -- what 'same shape' means."""
    out: set[str] = set()

    def walk(v: Any, prefix: str) -> None:
        if isinstance(v, dict):
            for k, child in v.items():
                out.add(f"{prefix}.{k}")
                walk(child, f"{prefix}.{k}")
        elif isinstance(v, list):
            for child in v:
                walk(child, f"{prefix}[]")

    walk(value, "")
    return out


@pytest.fixture
def produced(client: TestClient) -> dict[str, Any]:
    with SessionLocal() as session:
        session.query(AttentionItem).delete()
        session.commit()

    ha = FakeHA()
    siris = full_siris(home_assistant_base_url="http://ha", home_assistant_token="t")
    _wire_ha(siris, ha)
    siris.llm = FakeLLM()
    siris.second_brain = ExampleBrain()  # type: ignore[assignment]
    siris.attention._last_brain_sync = None
    parcel = siris.parcels.add("LX123456789AU", "New drill")
    siris.parcels.update(parcel["id"], status="InTransit", eta="2026-10-10")
    _fake(siris, "frigate_look_at_camera", {"camera": "front_door", "question": "Who's at the door?", "description": "A courier is holding a brown parcel."})
    switch = Recorder("home_assistant_call_service_fake", ToolPermission.CONFIRM_REQUIRED)
    siris.tools._tools[switch.name] = switch
    # The timeline reads the shared audit log: make sure it has an entry, so
    # the example's shape doesn't depend on what other tests ran first.
    siris.audit.record("docker_restart_container", {"container": "plex"}, result={"restarted": True}, duration_ms=1,
                       trigger="self-healing: watched container 'plex' is down", verified=True)
    app.dependency_overrides[get_siris] = lambda: siris
    try:
        out: dict[str, Any] = {}
        h = {"X-Siris-User": "brad", "X-Siris-Client": "SirisOS"}
        out["info"] = client.get("/siris/hub/v1", headers=h).json()
        out["widgets"] = client.get("/siris/hub/v1/widgets", headers=h).json()
        out["protocol_preview"] = client.get("/siris/hub/v1/protocols/lockdown", headers=h).json()
        out["protocol_run_request"] = {"confirmed": True}
        out["protocol_run"] = client.post("/siris/hub/v1/protocols/lockdown/run", json={"confirmed": True}, headers=h).json()
        out["camera_look_request"] = {"question": "Who's at the door?"}
        out["camera_look"] = client.post("/siris/hub/v1/cameras/front_door/look", json=out["camera_look_request"], headers=h).json()
        out["llm_complete_request"] = {"task": "sirishydro", "system": "Answer only from the evidence.", "prompt": "Question: minimum grade for a 225 mm sewer?\n[1] AS/NZS 3500.2 ..."}
        out["llm_complete"] = client.post("/siris/hub/v1/llm/complete", json=out["llm_complete_request"], headers=h).json()

        siris.memory.remember("Brad's drainage reports use Hazen-Williams for water mains", user_id="brad")
        out["search"] = client.get("/siris/hub/v1/search", params={"q": "Hazen-Williams"}, headers=h).json()
        siris.calendar = FakeCalendar()  # type: ignore[assignment]
        out["calendar"] = client.get("/siris/hub/v1/calendar", params={"start": "2026-10-12", "end": "2026-10-19"}, headers=h).json()
        out["forecast"] = client.get("/siris/hub/v1/forecast", params={"days": 2}, headers=h).json()

        notify_confirmation_needed(siris, "routine 'morning heat'", switch.name, {"domain": "switch", "service": "turn_on", "entity_id": "switch.heater"})
        siris.attention.raise_item("push:example", "alert", "SirisAI: plex needs you", source="self-healing", severity="urgent", body="plex is down and a restart didn't fix it.")
        out["brief"] = client.get("/siris/hub/v1/brief", headers=h).json()
        out["attention"] = client.get("/siris/hub/v1/attention", headers=h).json()
        confirm = next(i for i in out["attention"]["items"] if i["kind"] == "confirm")
        out["attention_act_request"] = {"action": "approve"}
        out["attention_act"] = client.post(f"/siris/hub/v1/attention/{confirm['id']}/act", json={"action": "approve"}, headers=h).json()
        stream = client.get("/siris/hub/v1/attention/stream", params={"max_events": 1}, headers=h).text
        out["attention_event"] = json.loads(next(line[6:] for line in stream.splitlines() if line.startswith("data: ")))
        return out
    finally:
        app.dependency_overrides.pop(get_siris, None)
        siris.parcels.update(parcel["id"], active=False)


def test_examples_match_what_the_endpoints_produce(produced: dict[str, Any]) -> None:
    for name, payload in produced.items():
        c.ENDPOINTS[name].model_validate(payload)
    if WRITE:
        EXAMPLES.mkdir(parents=True, exist_ok=True)
        for name, payload in produced.items():
            (EXAMPLES / f"{name}.json").write_text(json.dumps(payload, indent=2) + "\n")
        return
    for name, payload in produced.items():
        committed = json.loads((EXAMPLES / f"{name}.json").read_text())
        assert _keys(committed) == _keys(payload), f"{name}.json is stale: HUB_WRITE_EXAMPLES=1 pytest tests/test_hub_examples.py"
