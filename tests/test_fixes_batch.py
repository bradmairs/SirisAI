import json
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.core.config import Settings
from app.core.siris import SirisAI
from app.home_assistant.client import HomeAssistantClient
from app.plugins.builtin.home_assistant import HomeAssistantCreateAutomationTool, _normalize_automation_actions


def _siris() -> SirisAI:
    return SirisAI(settings=Settings(timezone="Australia/Melbourne"))


# -- entity memory: dedup, case-insensitive names, deletes (F9)


def test_a_reworded_fact_about_the_same_entity_is_not_saved_twice() -> None:
    siris = _siris()
    name = f"Widget-{uuid.uuid4()}"
    first = siris.entities.remember_fact(name, "A controllable smart light in the office.")
    second = siris.entities.remember_fact(name, "is a controllable smart light in the office")
    assert second["duplicate"] is True and second["id"] == first["id"]
    assert len(siris.entities.facts_about(name)) == 1


def test_genuinely_different_facts_are_both_kept() -> None:
    siris = _siris()
    name = f"Widget-{uuid.uuid4()}"
    siris.entities.remember_fact(name, "Lives in the study")
    siris.entities.remember_fact(name, "Bought in March 2024 for $600")
    assert len(siris.entities.facts_about(name)) == 2


def test_entity_names_match_case_insensitively() -> None:
    siris = _siris()
    tag = uuid.uuid4().hex[:8]
    siris.entities.remember_fact(f"NAS-{tag}", "Synology DS920 in the study")
    assert siris.entities.remember_fact(f"nas-{tag}", "Synology DS920 in the study")["duplicate"] is True
    assert len(siris.entities.facts_about(f"nas-{tag}")) == 1


def test_duplicate_relationships_are_not_saved_twice() -> None:
    siris = _siris()
    tag = uuid.uuid4().hex[:8]
    a = siris.entities.add_relationship(f"Plex-{tag}", "RUNS_ON", "main server")
    b = siris.entities.add_relationship(f"plex-{tag}", "runs_on", "Main Server")
    assert b["duplicate"] is True and b["id"] == a["id"]


def test_delete_tools_remove_facts_relationships_and_events() -> None:
    siris = _siris()
    tag = uuid.uuid4().hex[:8]
    fact = siris.entities.remember_fact(f"Thing-{tag}", "temporary fact")
    rel = siris.entities.add_relationship(f"Thing-{tag}", "USES", "Other")
    event = siris.events.log_event("temporary event", entity_name=f"Thing-{tag}")

    assert siris.run_tool("delete_entity_fact", {"fact_id": fact["id"]}) == {"deleted": True, "id": fact["id"]}
    assert siris.run_tool("delete_entity_relationship", {"relationship_id": rel["id"]})["deleted"] is True
    assert siris.run_tool("delete_event", {"event_id": event["id"]})["deleted"] is True
    assert siris.entities.facts_about(f"Thing-{tag}") == []
    with pytest.raises(ValueError):
        siris.run_tool("delete_entity_fact", {"fact_id": fact["id"]})


def test_recall_events_adds_local_time() -> None:
    siris = _siris()
    tag = f"probe-{uuid.uuid4().hex[:8]}"
    siris.events.log_event("something happened", entity_name=tag, created_at=datetime(2026, 9, 26, 2, 27))
    [event] = siris.run_tool("recall_events", {"entity_name": tag})
    assert event["at_local"] == "Sat 26 Sep 12:27"


# -- clock tool (F5)


def test_get_current_time_reports_weekday_and_zone() -> None:
    result = _siris().run_tool("get_current_time", {})
    assert result["timezone"] == "Australia/Melbourne"
    assert result["weekday"] in {"Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"}
    assert len(result["date"]) == 10


# -- audit and rollback (F11)


def test_explain_recent_action_without_a_tool_name_returns_recent_actions_not_lookups() -> None:
    siris = _siris()
    siris.run_tool("get_current_time", {})  # a READ, should be skipped
    siris.run_tool("remember", {"content": f"explain-probe {uuid.uuid4()}"})
    result = siris.run_tool("explain_recent_action", {})
    assert result and result[0]["tool_name"] == "remember"
    assert all(not str(e["policy"]).startswith("READ") for e in result)


def test_rollback_with_a_placeholder_tool_name_falls_back_to_the_latest_undoable_action() -> None:
    siris = _siris()
    result = siris.run_tool("rollback_last_action", {"tool_name": "Unspecified"}, confirmed=True)
    assert "rolled_back" in result  # a real answer, not an 'unsupported tool Unspecified' error


def test_trust_ledger_can_be_limited_to_recent_days() -> None:
    siris = _siris()
    assert siris.audit.verification_stats_by_tool(days=1) is not None
    assert isinstance(siris.run_tool("trust_ledger", {}), list)


# -- capability gap honesty (F11)


def test_reporting_a_gap_says_nothing_was_searched_or_built() -> None:
    result = _siris().run_tool(
        "report_capability_gap", {"requested_capability": f"test-only capability {uuid.uuid4()}"}
    )
    assert "Nothing was searched" in result["note"]


# -- integration notes (F6/F7)


def test_status_carries_integration_notes() -> None:
    siris = SirisAI(settings=Settings(docker_base_url="unix:///nope/docker.sock"))
    assert "docker" in siris.status()["integration_notes"]


# -- Home Assistant automations (F8)


def test_domain_and_service_are_joined_for_home_assistant() -> None:
    fixed = _normalize_automation_actions(
        [{"domain": "light", "service": "turn_off", "target": {"entity_id": "light.a"}}]
    )
    assert fixed == [{"service": "light.turn_off", "target": {"entity_id": "light.a"}}]


def test_already_correct_or_unrelated_actions_are_left_alone() -> None:
    good = [{"service": "light.turn_off", "target": {"entity_id": "light.a"}}, {"delay": "00:00:05"}, "odd"]
    assert _normalize_automation_actions(good) == good


def test_create_automation_sends_the_normalized_action_and_surfaces_ha_errors() -> None:
    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            sent.append(json.loads(request.content))
            return httpx.Response(200, json={"result": "ok"})
        return httpx.Response(200, json=[])

    client = HomeAssistantClient(base_url="http://ha.local:8123", token="t", transport=httpx.MockTransport(handler))
    tool = HomeAssistantCreateAutomationTool(client)
    tool.run(
        alias="x",
        trigger=[{"platform": "time", "at": "00:00:00"}],
        action=[{"domain": "light", "service": "turn_off", "target": {"entity_id": "light.a"}}],
    )
    assert sent[0]["action"][0]["service"] == "light.turn_off" and "domain" not in sent[0]["action"][0]

    def rejecting(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text='{"message":"Message malformed: extra keys not allowed @ data[\'action\'][0][\'domain\']"}')

    bad = HomeAssistantClient(base_url="http://ha.local:8123", token="t", transport=httpx.MockTransport(rejecting))
    with pytest.raises(RuntimeError, match="rejected this automation .*extra keys not allowed"):
        bad.create_automation("x", [], [])


def test_automation_verify_polls_until_the_entity_registers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.plugins.builtin.home_assistant._VERIFY_POLL_SECONDS", 0)
    lists = iter([[], [], [{"entity_id": "automation.x", "state": "on", "attributes": {"id": "123"}}]])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=next(lists))

    client = HomeAssistantClient(base_url="http://ha.local:8123", token="t", transport=httpx.MockTransport(handler))
    verification = HomeAssistantCreateAutomationTool(client).verify({}, {"automation_id": "123"})
    assert verification.verified is True


# -- speed test fallback ping (F11)


def test_ping_falls_back_to_tcp_connect_time_when_the_probe_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.speedtest import client as st
    from tests.test_speedtest import _FakeSpeedtest

    class _Probeless(_FakeSpeedtest):
        def __init__(self, calls: list[str], **kwargs: object) -> None:
            super().__init__(calls, **kwargs)
            self.results.ping = 1_800_000.0
            self.results.server["host"] = "speed.example.com:8080"

    monkeypatch.setattr(st, "_tcp_ping_ms", lambda host: 21.5 if host == "speed.example.com:8080" else None)
    result = st.SpeedTestClient(speedtest_factory=lambda **kw: _Probeless([], **kw)).run(direction="download")
    assert result["ping_ms"] == 21.5
    assert "TCP connect" in result["ping_note"]
