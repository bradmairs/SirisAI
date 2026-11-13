"""The hub's day endpoints for SirisOS: presence, reminders, emails with the
people in a meeting, upkeep, health by day, calendar attendees, and sending
an edited triage reply."""

from datetime import UTC, datetime, timedelta
from typing import Any

import icalendar

from app.core import guest
from app.calendar.ics import attendees
from app.hub import contract as c
from app.plugins.base import Tool, ToolPermission
from app.upkeep import backups
from tests.test_hub import FakeCalendar, _fake, _guest_off, full_siris, use  # noqa: F401 - fixtures


class FakeEmailClient:
    def __init__(self) -> None:
        self.asked: list[list[str]] = []

    def recent_with(self, addresses: list[str], limit: int = 5, since_days: int = 60) -> list[dict[str, Any]]:
        self.asked.append(addresses)
        return [{"uid": "41", "from": "Dana Lee <dana.lee@example.com>", "subject": "Werribee levels", "date": "Tue, 20 Oct 2026 09:12:00 +1100", "snippet": "Attached are the levels."}]

    def recent_about(self, words: list[str], limit: int = 5, since_days: int = 90) -> list[dict[str, Any]]:
        self.asked.append(words)
        return self.recent_with(words, limit)


class Sender(Tool):
    name = "email_send_reply"
    permission = ToolPermission.CONFIRM_REQUIRED
    parameters = {"type": "object", "properties": {}, "required": []}
    description = "fake"

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    def run(self, **kwargs: Any) -> Any:
        self.sent.append(kwargs)
        return {"sent": True}


def test_attendees_come_from_the_event_with_mailto_dropped() -> None:
    event = icalendar.Event()
    event.add("attendee", "mailto:dana.lee@example.com")
    event.add("attendee", "MAILTO:sam@example.com")
    event.add("organizer", "mailto:dana.lee@example.com")
    assert attendees(event) == ["dana.lee@example.com", "sam@example.com"]
    assert attendees(icalendar.Event()) == []


def test_calendar_range_includes_attendees(use) -> None:
    siris = full_siris()
    siris.calendar = FakeCalendar()  # type: ignore[assignment]
    body = use(siris).get("/siris/hub/v1/calendar", params={"start": "2026-10-12", "end": "2026-10-19"}).json()
    webinar = next(e for e in body["events"] if e["title"].startswith("Stormwater"))
    assert webinar["attendees"] == ["dana.lee@example.com", "brad@example.com"]
    assert next(e for e in body["events"] if e["title"] == "Bin night")["attendees"] == []


def test_presence_says_when_nobody_has_checked(use) -> None:
    siris = full_siris()
    client = use(siris)
    from app.skills.builtin.presence import _PRESENCE_ENTITY_NAME

    if not siris.events.recent_events(entity_name=_PRESENCE_ENTITY_NAME, limit=1):
        assert client.get("/siris/hub/v1/presence").json() == {"home": None, "checked_at": None, "since": None}
    siris.events.log_event("presence_changed", entity_name=_PRESENCE_ENTITY_NAME, data={"home": False})
    body = c.Presence.model_validate(client.get("/siris/hub/v1/presence").json())
    assert body.home is False and body.checked_at
    assert body.since == body.checked_at   # a real change: when it began
    siris.events.log_event("presence_changed", entity_name=_PRESENCE_ENTITY_NAME, data={"home": True, "baseline": True})
    assert client.get("/siris/hub/v1/presence").json()["since"] is None   # the first check isn't an arrival


def test_reminders_list_and_cancel(use) -> None:
    siris = full_siris()
    client = use(siris)
    made = siris.reminders.create("Put the bins out", datetime.now(UTC) + timedelta(hours=2))
    body = client.get("/siris/hub/v1/reminders").json()
    mine = next(r for r in body["reminders"] if r["id"] == made["id"])
    assert mine["message"] == "Put the bins out" and mine["status"] == "pending" and mine["due_at"].endswith("Z")
    assert client.post(f"/siris/hub/v1/reminders/{made['id']}/done").json() == {"id": made["id"], "status": "cancelled"}
    assert client.post(f"/siris/hub/v1/reminders/{made['id']}/done").status_code == 404
    assert all(r["id"] != made["id"] for r in client.get("/siris/hub/v1/reminders").json()["reminders"])
    guest.set_mode(siris, True)
    assert client.get("/siris/hub/v1/reminders").json() == {"guest_mode": True, "reminders": None}


def test_another_app_sets_a_reminder(use) -> None:
    """6.71: SirisOS sets pay day reminders; they're pushed when due like any other."""
    siris = full_siris()
    client = use(siris)
    r = client.post("/siris/hub/v1/reminders", json={"message": "Pay day: Salary", "due_at": "2026-11-20T08:00:00+11:00"})
    assert r.status_code == 200
    made = r.json()
    assert made["message"] == "Pay day: Salary" and made["due_at"].startswith("2026-11-19T21:00:00") and made["status"] == "pending"
    assert any(x["id"] == made["id"] for x in client.get("/siris/hub/v1/reminders").json()["reminders"])
    due = siris.reminders.due(datetime(2026, 11, 19, 21, 1, tzinfo=UTC))
    assert [d["id"] for d in due if d["id"] == made["id"]] == [made["id"]]
    assert client.post("/siris/hub/v1/reminders", json={"message": "x", "due_at": "2026-11-20T08:00:00"}).status_code == 422
    assert client.post("/siris/hub/v1/reminders", json={"message": "x", "due_at": "soon"}).status_code == 422


def test_email_with_reads_the_latest_from_those_people(use) -> None:
    siris = full_siris()
    client = use(siris)
    siris.tools._tools.pop("email_read", None)
    assert client.get("/siris/hub/v1/email/with", params={"address": "dana.lee@example.com"}).json() == {"guest_mode": False, "configured": False, "messages": None}
    fake = FakeEmailClient()
    _fake(siris, "email_read", {})._client = fake  # type: ignore[attr-defined]
    body = client.get("/siris/hub/v1/email/with", params=[("address", "dana.lee@example.com"), ("address", "sam@example.com")]).json()
    assert body["messages"] == [{"uid": "41", "sender": "Dana Lee", "subject": "Werribee levels", "date": "Tue, 20 Oct 2026 09:12:00 +1100", "snippet": "Attached are the levels."}]
    assert fake.asked == [["dana.lee@example.com", "sam@example.com"]]
    assert client.get("/siris/hub/v1/email/with").status_code == 422
    guest.set_mode(siris, True)
    assert client.get("/siris/hub/v1/email/with", params={"address": "x@example.com"}).json()["messages"] is None


def test_email_about_finds_a_project_by_subject(use) -> None:
    siris = full_siris()
    client = use(siris)
    fake = FakeEmailClient()
    _fake(siris, "email_read", {})._client = fake  # type: ignore[attr-defined]
    body = client.get("/siris/hub/v1/email/about", params=[("q", "Werribee Drainage"), ("q", "WD")]).json()
    assert body["messages"][0]["subject"] == "Werribee levels"
    assert fake.asked[-1] == ["Werribee Drainage", "WD"]
    assert client.get("/siris/hub/v1/email/about").status_code == 422
    guest.set_mode(siris, True)
    assert client.get("/siris/hub/v1/email/about", params={"q": "x"}).json()["messages"] is None


def test_subject_search_is_quoted_safely() -> None:
    from app.email.client import EmailClient

    seen: list[tuple] = []

    class Conn:
        def select(self, *a, **k): ...
        def uid(self, *a):
            seen.append(a)
            return "OK", [b""]
        def logout(self): ...

    client = EmailClient.__new__(EmailClient)
    client._imap = lambda: Conn()  # type: ignore[method-assign]
    assert client.recent_about(['Werribee "Drainage"\r\nX', "ab"]) == []
    assert [a[3] for a in seen] == ['"Werribee  Drainage   X"']


def test_upkeep_reports_backups_and_a_waiting_update(use, tmp_path, monkeypatch) -> None:
    siris = full_siris()
    client = use(siris)
    folder = tmp_path / "backups"
    folder.mkdir()
    (folder / f"{backups.PREFIX}2026-10-21{backups.SUFFIX}").write_bytes(b"x")
    monkeypatch.setattr(backups, "backup_dir", lambda settings: folder)
    monkeypatch.setattr(backups, "db_path", lambda settings: tmp_path / "siris.db")
    siris.attention.raise_item("update:9.9", "suggestion", "SirisAI 9.9 is out", source="updates", severity="info")
    body = c.Upkeep.model_validate(client.get("/siris/hub/v1/upkeep").json())
    assert body.backups is not None and body.backups.count == 1 and body.backups.newest
    assert body.update.available is True and body.update.latest == "9.9"


def test_health_days_and_running_distance(use) -> None:
    siris = full_siris()
    client = use(siris)
    today = datetime.now(UTC).date()
    assert client.post("/siris/health/metrics", json={"date": today.isoformat(), "metrics": {"run_km": 5.2, "steps": 9000}}).json()["stored"] == {"running_km": 5.2, "steps": 9000}
    body = client.get("/siris/hub/v1/health/days", params={"days": 3}).json()
    assert len(body["days"]) == 3 and body["days"][-1]["date"] == today.isoformat()
    year = client.get("/siris/hub/v1/health/days", params={"days": 366}).json()["days"]
    assert len(year) == 366 and year[-1]["metrics"]["running_km"] == 5.2  # SirisOS's year view (4.1)
    assert client.get("/siris/hub/v1/health/days", params={"days": 401}).status_code == 422
    assert body["days"][-1]["metrics"]["running_km"] == 5.2
    guest.set_mode(siris, True)
    assert client.get("/siris/hub/v1/health/days").json()["days"] is None


def test_a_triage_reply_can_be_sent_as_edited(use) -> None:
    siris = full_siris()
    client = use(siris)
    sender = Sender()
    siris.tools._tools[sender.name] = sender
    item = siris.attention.raise_item("triage:email:77", "review", "Reply to Dana: levels", source="triage", severity="attention",
                                      body="draft", actions=[{"id": "send", "label": "Send reply", "style": "primary"}],
                                      payload={"uid": "77", "body": "Thanks Dana, all good."})
    r = client.post(f"/siris/hub/v1/attention/{item['id']}/act", json={"action": "send", "body": "Thanks Dana, I'll check them tomorrow."})
    assert r.status_code == 200, r.text
    assert sender.sent[-1] == {"uid": "77", "body": "Thanks Dana, I'll check them tomorrow."}
