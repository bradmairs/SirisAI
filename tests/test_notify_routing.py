"""5.8: where a push goes -- straight out, held overnight, or spoken on the HUD at home."""

import time
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.siris import SirisAI
from app.notify import routing
from app.notify.routing import RoutedNotifyClient

DAY = datetime(2030, 1, 15, 3, 0, tzinfo=UTC)     # 2 pm in Melbourne
NIGHT = datetime(2030, 1, 15, 13, 0, tzinfo=UTC)  # midnight in Melbourne


class Inner:
    def __init__(self) -> None:
        self.sent: list[tuple] = []

    def send(self, message, title=None, actions=None, priority=None) -> None:
        self.sent.append((title, message, priority))

    def close(self) -> None:
        pass


@pytest.fixture
def setup(monkeypatch: pytest.MonkeyPatch):
    siris = SirisAI(settings=Settings(timezone="Australia/Melbourne", quiet_hours="22-7"))
    queued: list[tuple] = []
    monkeypatch.setattr(siris.pending_notifications, "queue", lambda m, sev, cat: queued.append((m, cat)))
    monkeypatch.setattr(routing, "is_home", lambda s: False)
    siris.state.set(routing.HUD_SEEN_KEY, 0)
    inner = Inner()
    return siris, RoutedNotifyClient(inner, siris), inner, queued


def test_daytime_pushes_go_out_as_before(setup) -> None:
    siris, router, inner, queued = setup
    assert router.send("Parcel delivered", title="Parcels", now=DAY) == "pushed"
    assert inner.sent == [("Parcels", "Parcel delivered", None)] and queued == []


def test_overnight_pushes_wait_for_the_morning_but_reminders_and_urgent_dont(setup) -> None:
    siris, router, inner, queued = setup
    assert router.send("Disk 85% full", title="Server", now=NIGHT) == "held"
    assert queued == [("Server: Disk 85% full", "quiet hours")] and inner.sent == []
    assert router.send("Take the bins out", title="Reminder", priority="high", now=NIGHT) == "pushed"
    assert router.send("Water leak!", priority="urgent", now=NIGHT) == "pushed"
    assert router.send("Approve?", actions=[{"action": "http"}], now=NIGHT) == "pushed"   # buttons need the phone


def test_quiet_hours_can_be_turned_off_or_not_wrap_midnight(setup) -> None:
    siris, router, inner, queued = setup
    siris.settings.quiet_hours = ""
    assert router.send("x", now=NIGHT) == "pushed"
    siris.settings.quiet_hours = "13-15"
    assert routing.in_quiet_hours(siris, DAY) is True and routing.in_quiet_hours(siris, NIGHT) is False


def test_at_home_with_the_hud_open_it_speaks_and_only_pushes_if_unheard(setup, monkeypatch: pytest.MonkeyPatch) -> None:
    siris, router, inner, queued = setup
    monkeypatch.setattr(routing, "is_home", lambda s: True)
    monkeypatch.setattr(routing, "SPOKEN_TIMEOUT", 0.05)
    routing.note_hud_seen(siris)
    assert router.send("The washing's done", title="Laundry", now=DAY) == "spoken"
    first = router.pending()[0]
    assert first["message"] == "The washing's done" and inner.sent == []
    assert router.heard(first["id"]) is True
    time.sleep(0.15)
    assert inner.sent == []                                   # heard: never pushed

    router.send("Front door open", now=DAY)                   # nobody confirms this one
    time.sleep(0.15)
    assert inner.sent == [(None, "Front door open", None)]    # so the phone gets it


def test_away_or_hud_closed_means_the_phone(setup, monkeypatch: pytest.MonkeyPatch) -> None:
    siris, router, inner, queued = setup
    monkeypatch.setattr(routing, "is_home", lambda s: True)
    siris.state.set(routing.HUD_SEEN_KEY, time.time() - 600)   # HUD last seen 10 minutes ago
    assert router.send("x", now=DAY) == "pushed"


def test_the_hud_reports_itself_and_confirms_what_it_said(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.siris import get_siris

    siris = get_siris()
    inner = Inner()
    siris._notify_client = RoutedNotifyClient(inner, siris)
    monkeypatch.setattr(routing, "is_home", lambda s: True)
    monkeypatch.setattr(routing, "in_quiet_hours", lambda s, now=None: False)
    client.get("/siris/hud/summary", params={"speaker": 1})
    assert routing.hud_listening(siris)
    siris._notify_client.send("Dinner's ready")
    got = client.get("/siris/hud/summary", params={"speaker": 1}).json()["announcements"]
    assert [a["message"] for a in got] == ["Dinner's ready"]
    assert client.post(f"/siris/hud/announcements/{got[0]['id']}/heard").json() == {"heard": True}
    assert client.get("/siris/hud/summary", params={"speaker": 1}).json()["announcements"] == []
    assert "announcements" not in client.get("/siris/hud/summary").json()   # SirisOS's widgets aren't speakers
