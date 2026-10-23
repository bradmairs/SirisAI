from datetime import UTC, datetime
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import imaplib
import pytest

from app.core.config import Settings
from app.core.siris import SirisAI
from app.gmail import importer
from app.second_brain import jobs

pytestmark = pytest.mark.usefixtures("client")  # creates the shared test database's tables

NOW = datetime(2026, 10, 22, 14, 0, tzinfo=UTC)   # 01:00 in Melbourne


def _email(subject: str = "Fwd: Mt Buller booking", body: str = "Your booking is confirmed for 14 June.",
           html: str = "", attach: bytes = b"") -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = "Mt Buller Resort <bookings@mtbuller.example>"
    msg["Subject"] = subject
    msg["Date"] = "Tue, 21 Oct 2026 09:30:00 +1100"
    if html:
        msg.set_content(html, subtype="html")
    else:
        msg.set_content(body)
    if attach:
        msg.add_attachment(attach, maintype="image", subtype="jpeg", filename="ticket.jpg")
    return msg


def test_an_email_becomes_a_note() -> None:
    saved = {}
    note = importer.message_to_note(_email(attach=b"\xff\xd8\xff jpg"), lambda name, data: saved.setdefault(name, data) and name)
    assert note["title"] == "Mt Buller booking"
    assert note["content"].startswith("*Emailed to Siris: from Mt Buller Resort (bookings@mtbuller.example), 2026-10-21.*")
    assert "Your booking is confirmed for 14 June." in note["content"]
    assert "![[2026-10-21 Mt Buller booking - ticket.jpg]]" in note["content"]
    assert note["source"] == "Email from Mt Buller Resort, 2026-10-21"


def test_html_mail_is_converted_and_quoted_replies_left_out() -> None:
    note = importer.message_to_note(_email(subject="Re: Re: Pump quote", html="<p>Thanks, <b>approved</b>.</p>"), lambda n, d: n)
    assert note["title"] == "Pump quote" and "Thanks, **approved**." in note["content"]
    reply = importer.message_to_note(_email(body="Sounds good.\n\nOn Mon, 20 Oct 2026, Dave wrote:\n> old text"), lambda n, d: n)
    assert "Sounds good." in reply["content"] and "old text" not in reply["content"]


class FakeIMAP:
    def __init__(self, messages: dict[int, bytes], validity: str = "7", password: str = "app-pass", label: bool = True) -> None:
        self.messages, self.validity, self.password, self.label = messages, validity, password, label
        self.readonly = None
        self.logged_out = False

    def login(self, user: str, password: str) -> None:
        if password != self.password:
            raise imaplib.IMAP4.error("[AUTHENTICATIONFAILED] Invalid credentials")

    def select(self, mailbox: str, readonly: bool = False) -> tuple[str, list]:
        self.readonly = readonly
        return ("OK", [b"2"]) if self.label else ("NO", [b"no such mailbox"])

    def response(self, code: str) -> tuple[str, list]:
        return code, [self.validity.encode()]

    def uid(self, command: str, *args: Any) -> tuple[str, list]:
        if command == "SEARCH":
            low = int(args[1].split()[1].split(":")[0])
            return "OK", [" ".join(str(u) for u in sorted(self.messages) if u >= low).encode()]
        return "OK", [(b"1 (BODY[] {10}", self.messages[int(args[0])]), b")"]

    def logout(self) -> None:
        self.logged_out = True


class FakeBrain:
    def __init__(self, path: Path) -> None:
        self.path, self.learned = path, []

    def learn(self, items: list[dict]) -> list[dict]:
        self.learned += items
        return [{"action": "created", "title": i["title"]} for i in items]


class FakeNotify:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, Any]] = []

    def send(self, message: str, title: str = "", **kwargs: Any) -> None:
        self.sent.append((title, message, kwargs.get("priority")))


def _siris(tmp_path: Path, **settings: Any) -> SirisAI:
    siris = SirisAI(settings=Settings(timezone="Australia/Melbourne", gmail_address="brad6491@gmail.com",
                                      gmail_app_password="app-pass", **settings))
    siris.state.set(importer.STATE_KEY, {})
    siris.state.set(importer.AUTH_ALERT_KEY, False)
    siris.second_brain = FakeBrain(tmp_path)
    siris._notify_client = FakeNotify()
    return siris


def test_labelled_emails_go_to_the_inbox_once(tmp_path: Path) -> None:
    siris = _siris(tmp_path)
    imap = FakeIMAP({3: _email().as_bytes(), 5: _email(subject="Receipt", attach=b"\xff\xd8\xff").as_bytes()})
    out = importer.run_import(siris, NOW, lambda: imap)
    assert out["imported"] == ["Mt Buller booking", "Receipt"]
    assert imap.readonly is True and imap.logged_out
    item = siris.second_brain.learned[0]
    assert item["inbox_if_new"] is True and item["tags"] == ["email"]
    assert (tmp_path / importer.ATTACHMENTS / "2026-10-21 Receipt - ticket.jpg").exists()
    assert importer.run_import(siris, NOW, lambda: imap)["imported"] == []          # already in
    imap.messages[9] = _email(subject="New one").as_bytes()
    assert importer.run_import(siris, NOW, lambda: imap)["imported"] == ["New one"]
    assert siris.state.get(importer.LAST_RUN_KEY)["date"] == "2026-10-23"


def test_a_reset_label_starts_over_and_the_nightly_budget_holds(tmp_path: Path) -> None:
    siris = _siris(tmp_path, gmail_max_messages=1)
    imap = FakeIMAP({1: _email(subject="A").as_bytes(), 2: _email(subject="B").as_bytes()})
    out = importer.run_import(siris, NOW, lambda: imap)
    assert out == {"imported": ["A"], "waiting": 1}
    imap.validity = "8"   # Gmail renumbered the label: start again from the top
    assert importer.run_import(siris, NOW, lambda: imap)["imported"] == ["A"]


def test_a_wrong_password_alerts_once_and_a_missing_label_is_skipped(tmp_path: Path) -> None:
    siris = _siris(tmp_path)
    for _ in range(2):
        out = importer.run_import(siris, NOW, lambda: FakeIMAP({}, password="other"))
    assert "error" in out
    assert len(siris._notify_client.sent) == 1 and siris._notify_client.sent[0][2] == "high"
    assert "skipped" in importer.run_import(siris, NOW, lambda: FakeIMAP({}, label=False))
    siris.settings.gmail_app_password = ""
    assert "skipped" in importer.run_import(siris, NOW)


def test_the_import_runs_nightly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    siris = _siris(tmp_path, brain_meeting_notes_hour_local=None, brain_dream_hour_local=None,
                   brain_upkeep_hour_local=None, brain_weekly_hour_local=None)
    siris.state.set(jobs.STATE_KEY, {})
    monkeypatch.setattr(importer, "run_import", lambda s, now: {"imported": ["A"]})
    assert asyncio.run(jobs.run_due(siris, NOW))["gmail"] == {"imported": ["A"]}
    assert asyncio.run(jobs.run_due(siris, NOW)) == {}
