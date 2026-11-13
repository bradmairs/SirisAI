"""6.38: actions followed up, the check after a new version, what SirisAI did
by area, and the attention feed out loud."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core import followup
from app.core.config import Settings
from app.core.siris import SirisAI
from app.db.models import AttentionItem
from app.db.session import SessionLocal
from app.plugins.base import Tool, ToolPermission, ToolVerification


@pytest.fixture(autouse=True)
def _clean(client: TestClient):  # type: ignore[no-untyped-def]
    with SessionLocal() as session:
        session.query(AttentionItem).delete()
        session.commit()
    followup.clear()
    yield
    followup.clear()


class Switch(Tool):
    name = "test_switch"
    description = "turns a test switch on"
    permission = ToolPermission.SAFE_ACTION
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, take_after: int | None = None) -> None:
        self.checks = 0
        self.take_after = take_after

    def run(self, **kwargs: Any) -> Any:
        return {"ok": True}

    def verify(self, arguments: dict[str, Any], result: Any) -> ToolVerification:
        self.checks += 1
        on = self.take_after is not None and self.checks > self.take_after
        return ToolVerification(verified=on, detail="'switch.test' is 'on'." if on else "'switch.test' is 'off', not the expected 'on'.")


def _feed(siris: SirisAI, source: str) -> dict[str, dict[str, Any]]:
    return {i["title"]: i for i in siris.attention.list() if i["source"] == source}


def test_an_action_that_didnt_take_is_checked_again_then_reported() -> None:
    siris = SirisAI(settings=Settings())
    tool = Switch()
    siris.tools.register(tool)
    siris.run_tool("test_switch", {}, trigger="chat")
    assert len(followup.pending()) == 1 and tool.checks == 1
    t0 = followup.pending()[0]["since"]
    assert followup.check(siris, t0 + 10) == []  # not due yet
    assert followup.check(siris, t0 + followup.FIRST_SECONDS + 1) == [] and tool.checks == 2  # once more later
    reported = followup.check(siris, t0 + followup.AGAIN_SECONDS + 1)
    assert len(reported) == 1 and followup.pending() == []
    item = _feed(siris, "followup")["That didn't happen: test switch"]
    assert item["kind"] == "alert" and "'switch.test' is 'off'" in item["body"]


def test_one_that_catches_up_says_nothing() -> None:
    siris = SirisAI(settings=Settings())
    tool = Switch(take_after=1)
    siris.tools.register(tool)
    siris.run_tool("test_switch", {})
    t0 = followup.pending()[0]["since"]
    assert followup.check(siris, t0 + followup.FIRST_SECONDS + 1) == [] and followup.pending() == []
    assert _feed(siris, "followup") == {}


def test_a_new_version_is_checked_once(monkeypatch: pytest.MonkeyPatch) -> None:
    from app import __version__
    from app.upkeep import release_check

    siris = SirisAI(settings=Settings(release_check_delay_seconds=0, llm_provider="none"))
    siris.state.set(release_check.STATE_KEY, {"version": "0.1", "down": []})

    class Health(Tool):
        name = "integration_health"
        description = "x"
        permission = ToolPermission.READ
        parameters = {"type": "object", "properties": {}, "required": []}

        def run(self, **kwargs: Any) -> Any:
            return {"down": ["Home Assistant"]}

    siris.tools._tools["integration_health"] = Health()
    assert release_check.due(siris)
    record = release_check.run(siris)
    assert record["problems"] == ["Home Assistant isn't reachable"] and not release_check.due(siris)
    alert = next(i for i in siris.attention.list() if i["title"].startswith(f"After updating to {__version__}"))
    assert "Home Assistant isn't reachable" in alert["title"] and "The last version was 0.1." in alert["body"]

    siris.state.set(release_check.STATE_KEY, {"version": "0.2", "down": []})
    siris.tools._tools["integration_health"] = type("Fine", (Health,), {"run": lambda self, **k: {"down": []}})()
    release_check.run(siris)
    assert any(i["title"] == f"Updated to {__version__}: everything checks out" for i in siris.attention.list())
    assert SirisAI(settings=Settings(release_check_delay_seconds=-1)).settings.release_check_delay_seconds == -1
    assert not release_check.due(SirisAI(settings=Settings(release_check_delay_seconds=-1)))


def test_what_i_did_is_grouped_by_area(client: TestClient) -> None:
    from app.audit.autonomy import timeline

    siris = SirisAI(settings=Settings())
    siris.tools.register(Switch(take_after=0))
    siris.run_tool("test_switch", {}, trigger="routine: 'evening' (at 18:00)")
    out = timeline(siris, hours=1)
    mine = [i for i in out["items"] if i["tool"] == "test_switch"]
    assert mine and mine[0]["area"] == "misc" and mine[0]["details"] == f"/siris/activity?id={mine[0]['id']}"
    assert any(g["area"] == "misc" for g in out["by_area"]) and "Other (" in out["summary"]
    one = client.get(f"/siris/activity?id={mine[0]['id']}").json()
    assert len(one) <= 1


def test_the_feed_out_loud_and_dismissing_by_number() -> None:
    from app.hub.attention import action
    from app.voice.confirmations import voice_confirmable

    siris = SirisAI(settings=Settings())
    siris.attention.raise_item("t:1", "suggestion", "2 updates waiting on nas", source="tests", severity="info",
                               actions=[action("install", "Install updates"), action("dismiss", "Not now")])
    siris.attention.raise_item("t:2", "alert", "The front door is open", source="tests", severity="urgent")
    out = siris.run_tool("attention_summary", {})
    assert out["text"].startswith("2 things need you, 1 urgent. First, urgent: The front door is open. You can: Got it. Second: 2 updates waiting on nas. "
                                  "You can: Install updates.")
    done = siris.run_tool("attention_act", {"number": 2, "action": "dismiss"})
    assert done["message"].startswith("Dismissed") and len([i for i in siris.attention.list() if i["source"] == "tests"]) == 1
    tool = siris.tools.get("attention_act")
    assert not tool.needs_confirmation({"action": "dismiss"}) and tool.needs_confirmation({"action": "install"})
    assert voice_confirmable("attention_act", {"action": "dismiss"}, set())
    assert not voice_confirmable("attention_act", {"action": "install"}, set())
    with pytest.raises(ValueError, match="already been dealt with"):
        siris.run_tool("attention_act", {"number": 2, "action": "dismiss"})
