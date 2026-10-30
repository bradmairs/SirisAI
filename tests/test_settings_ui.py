"""The web UI's Settings page (app/core/settings_overrides.py, /siris/settings)."""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core import approvals, guest, settings_overrides
from app.core.config import Settings
from app.core.siris import SirisAI, get_siris
from app.main import app


@pytest.fixture
def overrides(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "overrides.json"
    monkeypatch.setenv("SIRISAI_OVERRIDES_FILE", str(path))
    yield path
    app.dependency_overrides.pop(get_siris, None)


def test_settings_are_listed_without_secrets(client: TestClient, overrides: Path) -> None:
    siris = SirisAI(settings=Settings())
    app.dependency_overrides[get_siris] = lambda: siris
    page = client.get("/siris/settings").json()
    names = {s["name"]: s for s in page["settings"]}
    assert names["server_watch_minutes"]["type"] == "number" and names["server_watch_minutes"]["group"] == "Servers"
    assert names["ssh_allow_changes"]["type"] == "bool"
    assert names["ssh_hosts"]["restart"] is True
    assert not any(k in names for k in ("approval_pin", "api_key", "llm_api_key", "email_password"))
    assert page["pin_enabled"] is False


def test_saving_applies_live_persists_and_validates(client: TestClient, overrides: Path) -> None:
    siris = SirisAI(settings=Settings())
    app.dependency_overrides[get_siris] = lambda: siris
    out = client.put("/siris/settings", json={"changes": {"server_watch_minutes": 5, "briefing_hour_local": "", "ssh_hosts": "nas=brad@10.0.0.2"}})
    assert out.status_code == 200, out.text
    assert out.json()["needs_restart"] == ["ssh_hosts"]
    assert siris.settings.server_watch_minutes == 5 and siris.settings.briefing_hour_local is None
    assert json.loads(overrides.read_text())["server_watch_minutes"] == 5
    # The next start-up reads them (get_settings applies the file).
    assert Settings(**settings_overrides.load()).server_watch_minutes == 5

    bad = client.put("/siris/settings", json={"changes": {"server_watch_minutes": "often"}})
    assert bad.status_code == 422
    assert client.put("/siris/settings", json={"changes": {"approval_pin": "1"}}).status_code == 422
    assert client.put("/siris/settings", json={"changes": {"api_key": "x"}}).status_code == 422
    assert siris.settings.server_watch_minutes == 5

    # null goes back to .env / the default.
    client.put("/siris/settings", json={"changes": {"server_watch_minutes": None}})
    assert "server_watch_minutes" not in json.loads(overrides.read_text())
    assert siris.settings.server_watch_minutes == Settings.model_fields["server_watch_minutes"].default


def test_saving_needs_the_pin_and_not_in_guest_mode(client: TestClient, overrides: Path) -> None:
    siris = SirisAI(settings=Settings(approval_pin="2468"))
    siris.state.delete(approvals.FAILURES_KEY)
    app.dependency_overrides[get_siris] = lambda: siris
    change = {"changes": {"ssh_allow_changes": True}}
    assert client.put("/siris/settings", json=change).json()["detail"].startswith("pin_required")
    assert client.put("/siris/settings", json={**change, "pin": "1"}).json()["detail"].startswith("Wrong PIN")
    assert client.put("/siris/settings", json={**change, "pin": "2468"}).status_code == 200
    assert siris.settings.ssh_allow_changes is True

    guest.set_mode(siris, True, hours=1)
    try:
        assert client.put("/siris/settings", json={**change, "pin": "2468"}).status_code == 403
    finally:
        guest.set_mode(siris, False)


def test_changes_are_kept_and_can_be_undone(client: TestClient, overrides: Path) -> None:
    siris = SirisAI(settings=Settings())
    siris.state.delete(settings_overrides.HISTORY_KEY)
    app.dependency_overrides[get_siris] = lambda: siris
    default_watch = siris.settings.server_watch_minutes
    client.put("/siris/settings", json={"changes": {"server_watch_minutes": 5}})
    client.put("/siris/settings", json={"changes": {"server_watch_minutes": 9, "briefing_hour_local": ""}})
    changes = client.get("/siris/settings/history").json()["changes"]
    assert [c["id"] for c in changes] == [2, 1]
    assert changes[0]["changes"]["server_watch_minutes"] == {"before": 5.0, "was_set_here": True, "after": 9.0}
    assert changes[0]["labels"]["server_watch_minutes"] == "Server watch every (minutes)"

    # Undoing the second change puts back 5 (it was set here) and the briefing hour's .env value.
    out = client.post("/siris/settings/undo/2", json={})
    assert out.status_code == 200, out.text
    assert siris.settings.server_watch_minutes == 5
    stored = json.loads(overrides.read_text())
    assert "briefing_hour_local" not in stored and stored["server_watch_minutes"] == 5
    assert out.json()["change"]["undo_of"] == 2

    # Undoing the first one goes back to .env / the default, not to a pinned copy of it.
    client.post("/siris/settings/undo/1", json={})
    assert siris.settings.server_watch_minutes == default_watch
    assert "server_watch_minutes" not in json.loads(overrides.read_text())
    assert client.post("/siris/settings/undo/99", json={}).status_code == 404


def test_undo_needs_the_pin_too(client: TestClient, overrides: Path) -> None:
    siris = SirisAI(settings=Settings(approval_pin="2468"))
    siris.state.delete(approvals.FAILURES_KEY)
    siris.state.delete(settings_overrides.HISTORY_KEY)
    app.dependency_overrides[get_siris] = lambda: siris
    assert client.put("/siris/settings", json={"changes": {"server_watch_minutes": 5}, "pin": "2468"}).status_code == 200
    assert client.post("/siris/settings/undo/1", json={}).json()["detail"].startswith("pin_required")
    assert client.post("/siris/settings/undo/1", json={"pin": "2468"}).status_code == 200
