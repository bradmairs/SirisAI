"""The HUD page and data, and the autonomy timeline (app/audit/autonomy.py)."""

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from app.audit.autonomy import is_autonomous, timeline
from app.core.config import Settings
from app.core.siris import SirisAI, get_siris
from app.main import app


def test_is_autonomous_by_trigger() -> None:
    assert is_autonomous("self-healing: watched container 'plex' is down")
    assert is_autonomous("intruder_check: person detected while nobody was home")
    assert is_autonomous("energy scheduler: deferred job #1")
    assert is_autonomous("presence_check: everyone left")
    assert not is_autonomous("chat: model chose this tool while responding to the user's request")
    assert not is_autonomous("direct API request: a human explicitly asked for exactly this tool")
    assert not is_autonomous(None)


def test_timeline_lists_only_autonomous_actions_newest_first(client: TestClient) -> None:
    siris = SirisAI(settings=Settings(timezone="UTC"))
    start = datetime.now(UTC)
    siris.audit.record("notify", {"title": "SirisAI: plex needs you", "message": "plex is down"}, result=None, duration_ms=1, trigger="self-healing: watched container 'plex' is down")
    siris.audit.record("docker_restart_container", {"container": "plex"}, result={"restarted": True}, duration_ms=1, trigger="self-healing: watched container 'plex' is down", verified=True)
    siris.audit.record("notify", {"title": "hi", "message": "user asked"}, result=None, duration_ms=1, trigger="chat: model chose this tool")
    siris.audit.record("system_info", {}, result={}, duration_ms=1, trigger="proactive check: automated schedule")  # a READ

    result = timeline(siris, hours=1, now=start + timedelta(seconds=30))
    whats = [i["what"] for i in result["items"]]
    assert whats[:2] == ["Restarted plex", "Sent 'SirisAI: plex needs you': plex is down"]
    assert all("user asked" not in w for w in whats)
    assert all(i["tool"] != "system_info" for i in result["items"])
    assert result["items"][0]["verified"] is True
    assert result["summary"].startswith("In the last 1 hours I took")
    assert any(i["tool"] == "system_info" for i in timeline(siris, hours=1, include_reads=True, now=start + timedelta(seconds=30))["items"])


def test_what_did_you_do_tool_and_routes(client: TestClient) -> None:
    siris = SirisAI(settings=Settings())
    assert "summary" in siris.run_tool("what_did_you_do", {"hours": 2})
    app.dependency_overrides[get_siris] = lambda: siris
    try:
        assert client.get("/siris/autonomy/timeline?hours=2").status_code == 200
        summary = client.get("/siris/hud/summary").json()
        assert summary["system"]["cpu_percent"] is not None
        assert summary["guest_mode"] is not None and summary["energy"] is not None
        assert summary["weather"] is None and summary["cameras"] == []
    finally:
        app.dependency_overrides.pop(get_siris, None)


def test_hud_page_is_served(client: TestClient) -> None:
    response = client.get("/hud")
    assert response.status_code == 200
    assert "SirisAI HUD" in response.text and "/siris/voice/converse" in response.text
    assert 'href="/hud"' in client.get("/").text


def test_timeline_leaves_out_the_routine_watchers_but_keeps_what_they_did(client: TestClient) -> None:
    """geofence_reminder_check and presence_check run every few minutes and
    usually find nothing; the reminder they send is what belongs here."""
    siris = SirisAI(settings=Settings(timezone="UTC"))
    start = datetime.now(UTC)
    for _ in range(5):
        siris.audit.record("geofence_reminder_check", {}, result={"checked": True, "fired": []}, duration_ms=1, trigger="proactive check: automated schedule (geofence)")
        siris.audit.record("presence_check", {}, result={"changed": False}, duration_ms=1, trigger="proactive check: automated schedule (presence)")
    siris.audit.record("notify", {"title": "SirisAI: reminder", "message": "Take the bins out"}, result=None, duration_ms=1, trigger="geofence_reminder_check: person.brad entered zone 'home'")
    whats = [i["what"] for i in timeline(siris, hours=1, now=start + timedelta(seconds=30))["items"]]
    assert "Sent 'SirisAI: reminder': Take the bins out" in whats
    assert not any("eofence reminder check" in w or "resence check" in w for w in whats)
