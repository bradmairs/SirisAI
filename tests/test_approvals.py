"""The approval PIN and approvals that expire (app/core/approvals.py)."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core import approvals
from app.core.config import Settings
from app.core.siris import SirisAI, get_siris
from app.db.models import AttentionItem
from app.db.session import SessionLocal
from app.hub import attention
from app.main import app
from app.notify.confirmation import item_actions
from app.plugins.base import Tool, ToolPermission
from app.voice.confirmations import voice_confirmable

PIN = "2468"


class BigTool(Tool):
    name = "big_thing"
    description = "Does something big."
    permission = ToolPermission.CONFIRM_REQUIRED
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self) -> None:
        self.runs = 0

    def run(self, **kwargs: Any) -> Any:
        self.runs += 1
        return {"done": True}


def _siris(**settings: Any) -> tuple[SirisAI, BigTool]:
    siris = SirisAI(settings=Settings(**{"approval_pin": PIN, "approval_pin_tools": "big_thing", **settings}))
    tool = BigTool()
    siris.tools.register(tool)
    return siris, tool


@pytest.fixture(autouse=True)
def _clean(client: TestClient):
    with SessionLocal() as session:
        session.query(AttentionItem).delete()
        session.commit()
    SirisAI(settings=Settings()).state.delete(approvals.FAILURES_KEY)
    yield
    app.dependency_overrides.pop(get_siris, None)


def _age(item_id: int, minutes: float) -> None:
    with SessionLocal() as session:
        row = session.get(AttentionItem, item_id)
        row.updated_at = datetime.now(UTC) - timedelta(minutes=minutes)
        session.commit()


def test_a_high_risk_call_needs_the_pin_even_when_confirmed() -> None:
    siris, tool = _siris()
    with pytest.raises(approvals.PinRequired):
        siris.run_tool("big_thing", {}, confirmed=True)
    assert tool.runs == 0
    with approvals.pin_verified():
        siris.run_tool("big_thing", {}, confirmed=True)
    assert tool.runs == 1
    # The built-in list, and nothing at all without a PIN.
    assert {"ssh_power", "ssh_install_updates", "ssh_run"} <= approvals.high_risk_tools(siris)
    plain, plain_tool = _siris(approval_pin="")
    plain.run_tool("big_thing", {}, confirmed=True)
    assert plain_tool.runs == 1


def test_five_wrong_pins_lock_for_ten_minutes_and_alert() -> None:
    siris, _ = _siris()
    now = datetime.now(UTC)
    with pytest.raises(approvals.PinRequired):
        approvals.check_pin(siris, None)
    for n in range(4):
        with pytest.raises(approvals.PinRejected, match="Wrong PIN"):
            approvals.check_pin(siris, "0000", now)
    approvals.check_pin(siris, PIN, now)  # a right one resets the count
    for n in range(5):
        with pytest.raises(approvals.PinRejected):
            approvals.check_pin(siris, "0000", now)
    with pytest.raises(approvals.PinRejected, match="locked"):
        approvals.check_pin(siris, PIN, now + timedelta(minutes=5))
    approvals.check_pin(siris, PIN, now + timedelta(minutes=11))
    assert any(i["title"] == "Wrong PIN entered 5 times" for i in siris.attention.list())
    assert any(e["tool_name"] == "approval_pin" and e["error"] for e in siris.audit.list_recent(limit=10))


def test_approving_in_the_feed_needs_the_pin_and_declining_doesnt() -> None:
    siris, tool = _siris()
    item = attention.raise_confirmation(siris, "chat", "big_thing", {})
    with pytest.raises(approvals.PinRequired):
        attention.act(siris, item["id"], "approve")
    with pytest.raises(approvals.PinRejected):
        attention.act(siris, item["id"], "approve", pin="1111")
    assert tool.runs == 0
    closed, _ = attention.act(siris, item["id"], "approve", pin=PIN)
    assert tool.runs == 1 and closed["status"] != "open"

    other = attention.raise_confirmation(siris, "chat", "big_thing", {"again": 1})
    attention.act(siris, other["id"], "decline")
    assert tool.runs == 1

    # The server buttons that act directly need it too.
    reboot = {"source": "server-updates", "kind": "alert"}
    assert approvals.action_needs_pin(siris, reboot, "reboot", {})
    assert not approvals.action_needs_pin(siris, reboot, "dismiss", {})


def test_held_approvals_expire() -> None:
    siris, tool = _siris(approval_pin="", approval_expiry_minutes=15)
    item = attention.raise_confirmation(siris, "chat", "big_thing", {})
    _age(item["id"], 20)
    with pytest.raises(approvals.ApprovalExpired):
        attention.act(siris, item["id"], "approve")
    assert tool.runs == 0
    assert all(i["id"] != item["id"] for i in siris.attention.list("open"))

    fresh = attention.raise_confirmation(siris, "chat", "big_thing", {"n": 2})
    stale = attention.raise_confirmation(siris, "chat", "big_thing", {"n": 3})
    _age(stale["id"], 30)
    assert approvals.close_expired(siris) == 1
    assert [i["id"] for i in siris.attention.list("open")] == [fresh["id"]]

    never = SirisAI(settings=Settings(approval_expiry_minutes=0))
    assert not approvals.expired(never, {**stale, "updated_at": "2000-01-01T00:00:00"})


def test_phone_buttons_and_voice_never_approve_big_actions() -> None:
    siris, _ = _siris(public_base_url="https://siris.example")
    item = attention.raise_confirmation(siris, "chat", "big_thing", {})
    labels = [a["label"] for a in item_actions(siris, item) or []]
    assert "Approve" not in labels and labels
    for name in ("ssh_power", "ssh_install_updates", "ssh_run"):
        assert not voice_confirmable(name, {}, set())


def test_the_routes_ask_for_the_pin(client: TestClient) -> None:
    siris, tool = _siris()
    app.dependency_overrides[get_siris] = lambda: siris

    run = client.post("/siris/tools/big_thing/run", json={"confirmed": True})
    assert run.status_code == 403 and run.json()["detail"].startswith("pin_required")
    assert client.post("/siris/tools/big_thing/run", json={"confirmed": True, "pin": "9"}).json()["detail"].startswith("Wrong PIN")
    assert client.post("/siris/tools/big_thing/run", json={"confirmed": True, "pin": PIN}).status_code == 200
    assert tool.runs == 1
    # Not verified any more on the next request.
    assert client.post("/siris/tools/big_thing/run", json={"confirmed": True}).status_code == 403

    item = attention.raise_confirmation(siris, "chat", "big_thing", {})
    act = f"/siris/hub/v1/attention/{item['id']}/act"
    assert client.post(act, json={"action": "approve"}).json()["detail"].startswith("pin_required")
    assert client.post(act, json={"action": "approve", "pin": PIN}).status_code == 200
    assert tool.runs == 2

    old = attention.raise_confirmation(siris, "chat", "big_thing", {"old": True})
    _age(old["id"], 60)
    assert client.post(f"/siris/hub/v1/attention/{old['id']}/act", json={"action": "approve", "pin": PIN}).status_code == 410
    assert tool.runs == 2
