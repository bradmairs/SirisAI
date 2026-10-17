"""The attention feed (app/hub/attention.py, /siris/hub/v1/attention)."""

import json
import threading
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.anticipation import habits
from app.core.config import Settings
from app.core.siris import SirisAI, get_siris
from app.db.models import AttentionItem
from app.db.session import SessionLocal
from app.hub import attention
from app.hub import contract as c
from app.main import app
from app.notify.confirmation import notify_confirmation_needed
from app.plugins.base import Tool, ToolPermission


class Recorder(Tool):
    description = "fake"
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, name: str, permission: ToolPermission, result: Any = None) -> None:
        self.name = name
        self.permission = permission
        self.result = result if result is not None else {"ok": True}
        self.calls: list[dict[str, Any]] = []

    def run(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return self.result


class FakeNotifyClient:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    def send(self, message: str, **kwargs: Any) -> None:
        self.sent.append({"message": message, **kwargs})


@pytest.fixture(autouse=True)
def _empty_feed(client: TestClient):
    with SessionLocal() as session:
        session.query(AttentionItem).delete()
        session.commit()
    siris = SirisAI(settings=Settings())
    siris.state.delete(habits.STATE_KEY)
    for job in siris.deferred_jobs.list("waiting"):
        siris.deferred_jobs.cancel(job["id"])
    siris.pending_notifications.clear()
    yield


@pytest.fixture
def use(client: TestClient):
    def _use(siris: SirisAI) -> TestClient:
        app.dependency_overrides[get_siris] = lambda: siris
        return client

    yield _use
    app.dependency_overrides.pop(get_siris, None)


def _siris_with_notify() -> tuple[SirisAI, FakeNotifyClient]:
    from app.plugins.builtin.notify import NotifyTool

    siris = SirisAI(settings=Settings(timezone="UTC"))
    notify_client = FakeNotifyClient()
    siris.tools._tools["notify"] = NotifyTool(notify_client)  # type: ignore[arg-type]
    return siris, notify_client


def test_every_push_lands_in_the_feed_and_repeats_update_it() -> None:
    siris, sent = _siris_with_notify()
    siris.run_tool("notify", {"title": "SirisAI: plex needs you", "message": "plex is down", "priority": "high"}, trigger="self-healing: watched container 'plex' is down")
    siris.run_tool("notify", {"title": "SirisAI: plex needs you", "message": "plex is down", "priority": "high"}, trigger="self-healing: watched container 'plex' is down")
    items = siris.attention.list()
    assert len(sent.sent) == 2 and len(items) == 1
    item = items[0]
    assert item["kind"] == "alert" and item["severity"] == "urgent" and item["source"] == "self-healing"
    assert item["body"] == "plex is down" and [a["id"] for a in item["actions"]] == ["acknowledge"]
    c.AttentionItem.model_validate(item)


def test_an_intruder_alert_offers_lockdown() -> None:
    siris, _ = _siris_with_notify()
    siris.run_tool("notify", {"title": "SirisAI: person at front_door", "message": "Nobody's home."}, trigger="intruder_check: person detected while nobody was home")
    item = siris.attention.list()[0]
    assert item["severity"] == "urgent" and item["link"] == "cameras"
    assert [a["id"] for a in item["actions"]] == ["lockdown", "acknowledge"]
    assert item["actions"][0]["confirm"]


def test_unattended_confirmation_can_be_approved_from_the_feed(use) -> None:
    siris, sent = _siris_with_notify()
    switch = Recorder("fake_switch_tool", ToolPermission.CONFIRM_REQUIRED)
    siris.tools._tools[switch.name] = switch
    args = {"domain": "switch", "service": "turn_on", "entity_id": "switch.heater"}
    notify_confirmation_needed(siris, "routine 'morning heat'", switch.name, args)
    notify_confirmation_needed(siris, "routine 'morning heat'", switch.name, args)  # same request again: one item

    items = siris.attention.list()
    assert len(items) == 1 and items[0]["kind"] == "confirm"
    assert items[0]["title"].startswith("Approve:") and items[0]["source"] == "routine 'morning heat'"
    assert len(sent.sent) == 2  # the push still goes out (no attention item for it: it has actions)

    client = use(siris)
    done = client.post(f"/siris/hub/v1/attention/{items[0]['id']}/act", json={"action": "approve"}, headers={"X-Siris-Client": "SirisOS"})
    assert done.status_code == 200, done.text
    assert switch.calls == [args]
    assert done.json()["item"]["status"] == "done" and done.json()["item"]["outcome"] == f"Approved and ran {switch.name}"
    entry = next(a for a in siris.audit.list_recent(limit=10) if a["tool_name"] == switch.name and not a["confirmation_required"])
    assert entry["trigger"].startswith("attention feed: the user chose 'approve'") and "in SirisOS" in entry["trigger"]
    # Acting twice is a 404, not a second run.
    assert client.post(f"/siris/hub/v1/attention/{items[0]['id']}/act", json={"action": "approve"}).status_code == 404
    assert len(switch.calls) == 1


def test_decline_and_unknown_actions(use) -> None:
    siris, _ = _siris_with_notify()
    switch = Recorder("fake_switch_tool", ToolPermission.CONFIRM_REQUIRED)
    siris.tools._tools[switch.name] = switch
    notify_confirmation_needed(siris, "routine x", switch.name, {"domain": "switch", "service": "turn_on", "entity_id": "switch.a"})
    item = siris.attention.list()[0]
    client = use(siris)
    assert client.post(f"/siris/hub/v1/attention/{item['id']}/act", json={"action": "lockdown"}).status_code == 422
    declined = client.post(f"/siris/hub/v1/attention/{item['id']}/act", json={"action": "decline"}).json()["item"]
    assert declined["status"] == "dismissed" and switch.calls == []


def test_habit_suggestions_sync_accept_and_dismiss(use) -> None:
    siris = SirisAI(settings=Settings(timezone="UTC"))
    accept = Recorder("accept_habit_suggestion", ToolPermission.CONFIRM_REQUIRED, {"suggestion_id": "h1"})
    siris.tools._tools[accept.name] = accept
    siris.state.set(habits.STATE_KEY, {
        "h1": {"id": "h1", "description": "Kitchen light on at 6:45 on weekdays", "status": "suggested", "automation": {}},
        "h2": {"id": "h2", "description": "Hall light on at 18:00", "status": "suggested", "automation": {}},
    })
    client = use(siris)
    body = client.get("/siris/hub/v1/attention").json()
    c.AttentionList.model_validate(body)
    titles = {i["title"]: i for i in body["items"]}
    h1 = titles["Automate this? Kitchen light on at 6:45 on weekdays"]
    h2 = titles["Automate this? Hall light on at 18:00"]

    client.post(f"/siris/hub/v1/attention/{h1['id']}/act", json={"action": "accept"})
    assert accept.calls == [{"suggestion_id": "h1"}]
    client.post(f"/siris/hub/v1/attention/{h2['id']}/dismiss")
    assert habits.suggestions(siris, status=None)[1]["status"] == "dismissed"

    # A later sync doesn't bring either back, even though h1 is still
    # "suggested" in state (the fake accept didn't change it).
    after = client.get("/siris/hub/v1/attention").json()["items"]
    assert [i for i in after if i["source"] == "habits"] == []


def test_waiting_jobs_and_queued_notices_come_and_go() -> None:
    siris = SirisAI(settings=Settings(timezone="UTC"))
    job = siris.deferred_jobs.add("home_assistant_call_service", {"domain": "switch", "service": "turn_on", "entity_id": "switch.dishwasher"}, "run the dishwasher", True, None)
    siris.pending_notifications.queue("Disk usage on /data is 82%", severity="info", category="health")
    attention.sync_all(siris, include_brain=False)
    by_source = {i["source"]: i for i in siris.attention.list()}
    assert by_source["energy"]["title"] == "Waiting for cheap power: run the dishwasher"
    assert by_source["briefing"]["kind"] == "notice"

    item, result = attention.act(siris, by_source["energy"]["id"], "cancel")
    assert result == {"cancelled": True} and siris.deferred_jobs.list("waiting") == []

    siris.pending_notifications.clear()  # the briefing delivered it
    attention.sync_all(siris, include_brain=False)
    closed = siris.attention.list(status="done")
    assert any(i["source"] == "briefing" and i["outcome"] == "Delivered in the briefing" for i in closed)
    assert siris.attention.list() == []


class FakeBrain:
    def insights(self, days: int = 30) -> dict[str, Any]:
        return {"summary": {"inbox": 2}, "pending_auto_links": 3, "suggested_links": [{"a": "x", "b": "y"}]}


def test_second_brain_reviews() -> None:
    siris = SirisAI(settings=Settings(timezone="UTC"))
    siris.second_brain = FakeBrain()  # type: ignore[assignment]
    siris.attention._last_brain_sync = None
    attention.sync_all(siris)
    items = {i["key"]: i for i in siris.attention.list()}
    assert items["brain:links"]["body"] == "3 links about to be made automatically; 1 pair that could be linked."
    assert items["brain:links"]["link"] == "brain:links" and items["brain:links"]["actions"] == []
    assert items["brain:inbox"]["title"] == "2 notes in Siris' inbox"


def test_open_items_sort_urgent_first_and_counts() -> None:
    siris = SirisAI(settings=Settings())
    siris.attention.raise_item("a", "notice", "info thing", source="x", severity="info")
    siris.attention.raise_item("b", "alert", "urgent thing", source="x", severity="urgent")
    siris.attention.raise_item("c", "alert", "attention thing", source="x")
    assert [i["title"] for i in siris.attention.list()] == ["urgent thing", "attention thing", "info thing"]
    assert siris.attention.counts() == {"open": 3, "urgent": 1}


def test_stream_sends_a_snapshot_then_live_changes(use) -> None:
    siris = SirisAI(settings=Settings())
    siris.attention.raise_item("first", "alert", "Already here", source="test")
    client = use(siris)

    def later() -> None:
        # A change from another thread (as the scheduler would make) once the
        # stream has subscribed.
        import time

        for _ in range(100):
            if siris.attention._subscribers:
                break
            time.sleep(0.02)
        siris.attention.raise_item("second", "alert", "Just happened", source="test", severity="urgent")

    threading.Thread(target=later).start()
    response = client.get("/siris/hub/v1/attention/stream", params={"max_events": 2})
    assert response.headers["content-type"].startswith("text/event-stream")
    events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
    snapshot, upsert = (c.AttentionEvent.model_validate(e) for e in events)
    assert snapshot.type == "snapshot" and [i.title for i in snapshot.items] == ["Already here"]
    assert upsert.type == "upsert" and upsert.items[0].title == "Just happened"
    assert upsert.counts.urgent == 1
    assert siris.attention._subscribers == []  # cleaned up after the stream ended
