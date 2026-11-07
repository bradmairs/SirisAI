"""Life admin (4.6): automation drafts and their safety floor, approvals from
a push, conversation summaries and search, the document inbox, bills from
email, and travel mode."""

import asyncio
import io
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from app.automations import guard
from app.bills import service as bills
from app.conversation import summaries
from app.core.config import Settings
from app.core.siris import SirisAI
from app.documents import service as documents
from app.hub import attention
from app.llm.base import LLMClient, LLMResponse
from app.notify import confirmation
from app.travel import service as travel


# -- automations -------------------------------------------------------------


def test_an_automation_is_described_in_plain_english() -> None:
    text = guard.summarize({
        "alias": "Garage left open",
        "trigger": [{"platform": "state", "entity_id": "cover.garage_door", "to": "open", "for": "00:10:00"}],
        "condition": [{"condition": "state", "entity_id": "group.family", "state": "not_home"}],
        "action": [{"service": "notify.mobile_app_brads_iphone", "data": {"message": "The garage is open"}}],
    })
    assert text == 'When garage door becomes open, if family is not_home, notify mobile app brads iphone "The garage is open".'


@pytest.mark.parametrize(
    "actions",
    [
        [{"service": "lock.unlock", "target": {"entity_id": "lock.front_door"}}],
        [{"action": "cover.open_cover", "target": {"entity_id": "cover.garage"}}],
        [{"choose": [{"sequence": [{"service": "alarm_control_panel.alarm_disarm"}]}]}],
        [{"service": "homeassistant.toggle", "target": {"entity_id": "lock.back_door"}}],
    ],
)
def test_an_automation_may_never_unlock_open_or_disarm(actions: list[dict]) -> None:
    with pytest.raises(ValueError, match="can't do this by itself"):
        guard.check(actions)


def test_safe_automations_pass() -> None:
    guard.check([{"service": "light.turn_on", "target": {"entity_id": "light.hall"}}, {"service": "lock.lock"}])


def test_the_tool_refuses_before_creating_and_previews() -> None:
    from app.plugins.builtin.home_assistant import HomeAssistantCreateAutomationTool

    class HA:
        created = False

        def create_automation(self, *a: Any, **k: Any) -> dict:
            HA.created = True
            return {"automation_id": "x"}

    tool = HomeAssistantCreateAutomationTool(HA())  # type: ignore[arg-type]
    bad = {"alias": "Let in", "trigger": [{"platform": "time", "at": "08:00"}], "action": [{"service": "lock.unlock"}]}
    with pytest.raises(ValueError):
        tool.run(**bad)
    assert HA.created is False
    preview = tool.dry_run(**bad)
    assert preview["refused"] == ["lock.unlock"] and preview["summary"].startswith("When it's 08:00")


def test_the_approval_says_what_the_automation_does() -> None:
    from app.voice.confirmations import describe_call

    text = describe_call("home_assistant_create_automation", {
        "alias": "Porch at sunset", "trigger": [{"platform": "sun", "event": "sunset"}],
        "action": [{"service": "light.turn_on", "target": {"entity_id": "light.porch"}}],
    })
    assert text == 'add the automation "Porch at sunset": When at sunset, turn on porch.'


# -- approvals from a push ----------------------------------------------------


def test_a_pin_approval_gets_a_button_into_sirisos() -> None:
    siris = SirisAI(settings=Settings(public_base_url="https://ai.example", sirisos_public_url="https://os.example", approval_pin="1234"))
    item = attention.raise_confirmation(siris, "chat", "ssh_power", {"action": "reboot", "servers": "nas"})
    assert item is not None
    buttons = confirmation.item_actions(siris, item)
    assert buttons is not None and buttons[0] == {
        "action": "view", "label": "Approve in SirisOS", "url": f"https://os.example/?attention={item['id']}", "clear": True,
    }
    assert [b["label"] for b in buttons[1:]] == ["Decline"]


def test_the_push_says_what_it_wants_to_do() -> None:
    siris = SirisAI(settings=Settings())
    sent: list[dict] = []
    siris.tools.get = lambda name: object() if name == "notify" else None  # type: ignore[method-assign]
    siris.run_tool = lambda name, args, **k: sent.append(args)  # type: ignore[method-assign]
    confirmation.notify_confirmation_needed(siris, "SirisAI", "home_assistant_call_service", {"domain": "lock", "service": "unlock", "entity_id": "lock.front_door"})
    assert sent[0]["message"] == "SirisAI wants to unlock front door. It needs your OK."


# -- conversations ------------------------------------------------------------


class JsonLLM(LLMClient):
    def __init__(self, answer: dict[str, Any]) -> None:
        self.answer = answer
        self.calls: list[dict] = []

    async def generate(self, messages, *, tools=None, **kwargs) -> LLMResponse:  # type: ignore[no-untyped-def]
        self.calls.append({"messages": messages, **kwargs})
        return LLMResponse(text=json.dumps(self.answer))


def _old_conversation(siris: SirisAI, lines: list[tuple[str, str]], hours_ago: float) -> str:
    from app.db.models import Message

    cid = siris.conversation.create_conversation()
    at = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=hours_ago)
    with siris.conversation._session_factory() as session:
        for i, (role, text) in enumerate(lines):
            session.add(Message(conversation_id=cid, role=role, content=text, created_at=at + timedelta(seconds=i)))
        session.commit()
    return cid


def test_quiet_conversations_get_a_title_and_summary_from_the_local_model() -> None:
    siris = SirisAI(settings=Settings(llm_provider="ollama"))
    llm = JsonLLM({"title": "NAS backup schedule", "summary": "Asked how often the NAS backs up; it's nightly at 2am."})
    siris.llm = llm
    tag = uuid.uuid4().hex[:8]
    cid = _old_conversation(siris, [("user", f"how often does the nas{tag} back up?"), ("assistant", "Nightly at 2am.")], hours_ago=2)
    recent = _old_conversation(siris, [("user", "still talking"), ("assistant", "yes")], hours_ago=0)
    assert cid in summaries.due(siris, limit=500) and recent not in summaries.due(siris, limit=500)
    asyncio.run(summaries.summarize(siris, cid))
    assert llm.calls[0]["format"] == "json"
    assert cid not in summaries.due(siris, limit=500)
    found = summaries.search(siris, f"nas{tag}")
    assert found[0]["conversation_id"] == cid and found[0]["title"] == "NAS backup schedule"
    assert found[0]["matches"] == [f"how often does the nas{tag} back up?"]


def test_without_a_local_model_the_title_is_the_opening_request() -> None:
    siris = SirisAI(settings=Settings(llm_provider="none"))
    cid = _old_conversation(siris, [("user", "plan the garden beds"), ("assistant", "Sure.")], hours_ago=3)
    result = asyncio.run(summaries.summarize(siris, cid))
    assert result == {"conversation_id": cid, "title": "plan the garden beds", "summary": ""}


def test_search_by_date(client) -> None:  # type: ignore[no-untyped-def]
    siris = SirisAI(settings=Settings())
    tag = uuid.uuid4().hex[:8]
    old = _old_conversation(siris, [("user", f"tell me about {tag} last week"), ("assistant", "ok")], hours_ago=24 * 6)
    new = _old_conversation(siris, [("user", f"tell me about {tag} today"), ("assistant", "ok")], hours_ago=1)
    today = datetime.now(UTC).date().isoformat()
    ids = [c["conversation_id"] for c in summaries.search(siris, tag, since=today)]
    assert new in ids and old not in ids
    week_ago = (datetime.now(UTC) - timedelta(days=6)).date().isoformat()
    r = client.get("/siris/conversations/search", params={"q": tag, "since": week_ago, "until": week_ago})
    assert r.status_code == 200 and [c["conversation_id"] for c in r.json()] == [old]
    assert client.get("/siris/conversations/search", params={"since": "last tuesday"}).status_code == 422


# -- documents ------------------------------------------------------------------


def _pdf(text: str) -> bytes:
    """A one-page PDF with `text` on it (no extra dependency)."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objects, 1):
        offsets.append(out.tell())
        out.write(f"{i} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets:
        out.write(f"{offset:010d} 00000 n \n".encode())
    out.write(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode())
    return out.getvalue()


def test_due_dates_and_amounts_without_a_model() -> None:
    found = documents.find_due("Rates notice. Amount $412.50. Please pay by 14 November 2026. Ref 9921.")
    assert found == [{"date": "2026-11-14", "what": "pay by 14 November 2026", "amount": 412.5}]
    assert documents.find_due("Your appointment is on 3/12/26 at 10am")[0]["date"] == "2026-12-03"


def test_a_pdf_letter_is_filed_and_reminded(client) -> None:  # type: ignore[no-untyped-def]
    from app.core.siris import get_siris

    siris = get_siris()
    siris.settings.llm_provider = "none"
    due = (datetime.now(UTC) + timedelta(days=20)).date()
    pdf = _pdf(f"City Council rates. Amount due $412.50. Pay by {due.day} {due.strftime('%B')} {due.year}.")
    r = client.post("/siris/documents", files={"file": ("rates-notice.pdf", pdf, "application/pdf")})
    assert r.status_code == 200, r.text
    doc = r.json()
    assert doc["title"] == "rates notice" and doc["due"][0]["date"] == due.isoformat() and doc["due"][0]["amount"] == 412.5
    assert doc["reminders"] == 2 and "City Council" in doc["text"]
    assert any(d["id"] == doc["id"] for d in client.get("/siris/documents", params={"q": "council"}).json())
    assert siris.run_tool("documents_search", {"query": "council"})[0]["id"] == doc["id"]
    assert client.delete(f"/siris/documents/{doc['id']}").status_code == 200
    assert client.get(f"/siris/documents/{doc['id']}").status_code == 404


def test_the_local_model_reads_the_letter() -> None:
    siris = SirisAI(settings=Settings(llm_provider="ollama"))
    siris.llm = JsonLLM({"title": "Car rego renewal", "sender": "VicRoads", "kind": "bill", "summary": "Rego is due.",
                         "due": [{"date": "2026-12-01", "what": "Pay registration", "amount": 880}]})
    result = asyncio.run(documents.understand(siris, "VicRoads registration renewal ...", "scan.txt"))
    assert result == {"title": "Car rego renewal", "sender": "VicRoads", "kind": "bill", "summary": "Rego is due.",
                      "due": [{"date": "2026-12-01", "what": "Pay registration", "amount": 880.0}], "date": None, "items": []}


def test_unreadable_documents_say_why(client) -> None:  # type: ignore[no-untyped-def]
    r = client.post("/siris/documents", files={"file": ("photo.jpg", b"\xff\xd8 not really", "image/jpeg")})
    assert r.status_code == 422 and "vision model" in r.json()["detail"]
    r = client.post("/siris/documents", files={"file": ("x.zip", b"PK", "application/zip")})
    assert r.status_code == 422


# -- bills ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("subject", "body", "expected"),
    [
        ("Your Netflix membership", "Your membership will renew on 9 November 2026 for $22.99.",
         {"kind": "renewal", "amount": 22.99, "due_date": "2026-11-09"}),
        ("Your free trial ends soon", "Your trial ends on Nov 12, 2026. After your trial you'll be charged $14.99/month.",
         {"kind": "trial_ending", "amount": 14.99, "due_date": "2026-11-12"}),
        ("A change to your plan", "We're increasing the price of your plan from $79.00 to $85.00 starting 1 December 2026.",
         {"kind": "price_rise", "old_amount": 79.0, "amount": 85.0, "due_date": "2026-12-01"}),
        ("Your electricity bill", "Amount due $312.40. Due date 20/11/2026.",
         {"kind": "bill_due", "amount": 312.4, "due_date": "2026-11-20"}),
    ],
)
def test_classifies_bills(subject: str, body: str, expected: dict) -> None:
    found = bills.classify('"Acme Billing" <billing@acme.com>', subject, body)
    assert found is not None
    assert {k: found[k] for k in expected} == expected
    assert found["service"] == "Acme"


def test_leaves_receipts_and_newsletters() -> None:
    assert bills.classify("shop@x.com", "Receipt", "Thank you for your payment. Payment received: $40.00") is None
    assert bills.classify("news@x.com", "This week's news", "Our favourite recipes") is None


class FakeMail:
    def __init__(self, messages: list[dict[str, str]]) -> None:
        self.messages = messages

    def recent_about(self, words: list[str], limit: int = 5, since_days: int = 90) -> list[dict]:
        return [{k: m[k] for k in ("uid", "from", "subject", "date")} for m in self.messages]

    def read(self, uid: str) -> dict:
        return {"body": next(m["body"] for m in self.messages if m["uid"] == uid)}


def test_scan_raises_whats_coming_up_and_done_closes_it() -> None:
    siris = SirisAI(settings=Settings(timezone="UTC", bills_warn_days=4))
    now = datetime(2026, 11, 6, 9, tzinfo=UTC)
    tag = uuid.uuid4().hex[:6]
    mail = FakeMail([
        {"uid": "1", "from": f"Stream{tag} <no-reply@stream{tag}.com>", "subject": f"Stream{tag} renews soon",
         "date": "Thu, 05 Nov 2026 10:00:00 +0000", "body": "Your subscription will renew on 8 November 2026 for $19.99."},
        {"uid": "2", "from": f"Gym{tag} <hi@gym{tag}.com>", "subject": "Membership renewal notice",
         "date": "Thu, 05 Nov 2026 11:00:00 +0000", "body": "Your membership will renew on 30 November 2026 for $60.00."},
    ])
    result = bills.scan(siris, client=mail, now=now)
    assert result["added"] == 2
    assert bills.scan(siris, client=mail, now=now)["added"] == 0  # the same emails aren't added twice
    open_items = [i for i in siris.attention.list() if i["source"] == "bills" and tag in i["title"]]
    assert [i["title"] for i in open_items] == [f"Stream{tag} renews Sun 8 Nov for $19.99"]
    attention.act(siris, open_items[0]["id"], "done")
    assert not [i for i in siris.attention.list() if i["source"] == "bills" and tag in i["title"]]
    assert all(b["service"] != f"Stream{tag}" for b in bills.upcoming(siris, now=now))


# -- travel -------------------------------------------------------------------------


class FakeCalendar:
    def __init__(self, events: list[dict[str, Any]]) -> None:
        self.events = events

    def events_between(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        return self.events


def test_finds_trips_and_builds_the_lists(monkeypatch: pytest.MonkeyPatch) -> None:
    siris = SirisAI(settings=Settings(timezone="Australia/Melbourne", travel_home_city="Melbourne"))
    siris.calendar = FakeCalendar([  # type: ignore[assignment]
        {"summary": "Flight to Sydney", "start": "2026-11-08T07:00:00+11:00", "end": "2026-11-08T08:30:00+11:00", "location": None},
        {"summary": "Hotel", "start": "2026-11-08", "end": "2026-11-11", "location": "Sydney, NSW"},
        {"summary": "Dentist", "start": "2026-11-09T10:00:00+11:00", "end": "2026-11-09T11:00:00+11:00", "location": "Melbourne"},
        {"summary": "Work offsite", "start": "2026-11-20", "end": "2026-11-23", "location": "Carlton, Melbourne"},
    ])

    def handler(request: httpx.Request) -> httpx.Response:
        if "geocoding" in request.url.host:
            return httpx.Response(200, json={"results": [{"latitude": -33.87, "longitude": 151.2}]})
        return httpx.Response(200, json={"daily": {"time": ["2026-11-08", "2026-11-09"], "temperature_2m_max": [28, 30],
                                                   "temperature_2m_min": [18, 19], "precipitation_probability_max": [10, 60]}})

    monkeypatch.setattr(travel, "transport", httpx.MockTransport(handler))
    now = datetime(2026, 11, 6, 8, tzinfo=UTC)
    trips = travel.find_trips(siris, now)
    assert [(t["destination"], t["nights"]) for t in trips] == [("Sydney", 3)]
    planned = travel.plan(siris, trips[0])
    assert {"ID or passport", "Sunscreen", "Umbrella or rain jacket"} <= set(planned["packing"])

    wanted = travel.check(siris, now)
    assert wanted[0]["title"] == "Sydney on Sunday 8 November, 3 nights"
    assert "Weather there: 18 to 30°C" in wanted[0]["body"]
    item = next(i for i in siris.attention.list() if i["source"] == "travel")
    attention.act(siris, item["id"], "away")
    assert travel.away_active(siris, datetime(2026, 11, 9, tzinfo=UTC))
    assert not travel.away_active(siris, datetime(2026, 11, 20, tzinfo=UTC))
    siris.state.set(travel.AWAY_KEY, {})


def test_a_trip_makes_away_mode_run_by_itself(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.household import away

    siris = SirisAI(settings=Settings(away_mode="ask"))
    seen: list[str] = []
    monkeypatch.setattr(away, "left", lambda s, mode, now=None: seen.append(mode))
    now = datetime(2026, 11, 9, tzinfo=UTC)
    away.on_change(siris, home=False, now=now)
    siris.state.set(travel.AWAY_KEY, {"start": "2026-11-08T07:00:00+11:00", "end": "2026-11-11T00:00:00+11:00", "trip": "x"})
    away.on_change(siris, home=False, now=now)
    siris.state.set(travel.AWAY_KEY, {})
    assert seen == ["ask", "auto"]


# -- things from receipts in email ---------------------------------------------


class ReceiptMail:
    def __init__(self, messages: list[dict[str, Any]]) -> None:
        self.messages = messages
        self.reads: list[str] = []

    def find_everywhere(self, phrases: list[str], limit: int = 300, since_days: int = 30, label: str = "") -> list[dict]:
        return [{"filed": m.get("filed", False), **{k: m[k] for k in ("uid", "from", "subject", "date")}} for m in self.messages]

    def read_everywhere(self, uid: str) -> dict:
        self.reads.append(uid)
        m = next(m for m in self.messages if m["uid"] == uid)
        return {"uid": uid, "body": m["body"], "html": m.get("html", ""), "files": m.get("files", [])}


class ReceiptLLM(LLMClient):
    def __init__(self, answers: dict[str, dict[str, Any]]) -> None:
        self.answers = answers

    async def generate(self, messages, *, tools=None, **kwargs) -> LLMResponse:  # type: ignore[no-untyped-def]
        text = messages[-1]["content"]
        return LLMResponse(text=json.dumps(next((a for k, a in self.answers.items() if k in text), {"items": []})))


def _receipt_setup(monkeypatch: pytest.MonkeyPatch, answers: dict[str, dict[str, Any]]) -> SirisAI:
    from app.llm import local

    monkeypatch.setattr(local, "client", lambda siris: ReceiptLLM(answers))
    return SirisAI(settings=Settings(timezone="UTC"))


@pytest.fixture(autouse=True)
def _receipts_in_a_temp_folder(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    monkeypatch.setattr(documents, "files_dir", lambda siris, doc_id=None: tmp_path / "receipts" / str(doc_id) if doc_id is not None else tmp_path / "receipts")


def test_a_receipt_in_email_is_kept_with_its_originals_and_linked(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.things import from_email, service as things

    tag = uuid.uuid4().hex[:6]
    siris = _receipt_setup(monkeypatch, {f"Order {tag}": {"is_receipt": True, "title": f"JB {tag}", "shop": "JB Hi-Fi", "date": "2026-10-20", "total": 899, "items": [{"name": f"Speaker {tag}", "price": 899}]}})
    mail = ReceiptMail([{"uid": "1", "from": "orders@jbhifi.com.au", "subject": f"Order {tag} receipt", "date": "Wed, 21 Oct 2026 10:00:00 +0000", "body": "Thanks",
                         "html": "<b>Thanks</b><script>steal()</script>", "files": [{"filename": "../in voice.pdf", "data": b"not really a pdf"}]}])
    result = asyncio.run(from_email.scan(siris, client=mail))
    assert result["kept"] == 1 and result["added"] == 1
    doc = documents.get(siris, result["receipts"][0]["id"])
    assert doc["kind"] == "receipt" and doc["summary"] == "JB Hi-Fi, 20 Oct 2026, $899.00" and doc["note_title"] is None
    assert doc["files"] == ["_in voice.pdf", "email.html"]
    html = documents.file_path(siris, doc["id"], "email.html").read_text()
    assert "default-src 'none'" in html.split("<b>")[0]
    with pytest.raises(KeyError):
        documents.file_path(siris, doc["id"], "../../sirisai.db")
    thing = things.find(siris, f"Speaker {tag}")[0]
    assert thing["document_id"] == doc["id"]
    assert doc["id"] in [d["id"] for d in documents.list_documents(siris, kind="receipt", limit=300)]
    assert doc["id"] not in [d["id"] for d in documents.list_documents(siris, not_kind="receipt", limit=300)]

    documents.delete(siris, doc["id"])
    assert things.find(siris, f"Speaker {tag}")[0]["document_id"] is None, "the thing stays"
    assert asyncio.run(from_email.scan(siris, client=mail))["kept"] == 0, "a receipt you removed doesn't come back"
    things.delete(siris, thing["id"])


def test_things_added_earlier_get_their_receipt_and_arent_added_again(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.things import from_email, service as things

    tag = uuid.uuid4().hex[:6]
    siris = _receipt_setup(monkeypatch, {f"Order {tag}": {"is_receipt": True, "date": "2026-10-20", "items": [{"name": f"Router {tag}"}]}})
    message = {"uid": "1", "from": "shop@x.com", "subject": f"Order {tag} receipt", "date": "Wed, 21 Oct 2026 10:00:00 +0000", "body": "x"}
    old = things.add(siris, {"name": f"Router {tag}", "bought": "2026-10-20", "notes": from_email._note(message["subject"])})
    siris.state.set(from_email.SEEN_KEY, [*(siris.state.get(from_email.SEEN_KEY) or []), from_email._key(message)])
    result = asyncio.run(from_email.scan(siris, client=ReceiptMail([message])))
    assert result["kept"] == 1 and result["linked"] == 1 and result["added"] == 0
    assert things.find(siris, f"Router {tag}")[0]["document_id"] == result["receipts"][0]["id"]
    things.delete(siris, old["id"])


def test_anything_in_the_receipts_label_is_kept_whatever_it_says(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.things import from_email

    tag = uuid.uuid4().hex[:6]
    siris = _receipt_setup(monkeypatch, {tag: {"is_receipt": False, "items": []}, "advert": {"is_receipt": False, "items": []}})
    mail = ReceiptMail([
        {"uid": "1", "from": "Uber <noreply@uber.com>", "subject": f"Trip {tag} shipped", "date": "Wed, 21 Oct 2026 10:00:00 +0000", "body": "x", "filed": True},
        {"uid": "2", "from": "shop@x.com", "subject": f"Purchase advert {tag}", "date": "Wed, 21 Oct 2026 10:00:00 +0000", "body": "x"},
    ])
    result = asyncio.run(from_email.scan(siris, client=mail))
    assert result["kept"] == 1 and result["read"] == 2 and mail.reads[0] == "1"


def test_a_receipt_in_email_becomes_a_thing_once(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.things import from_email, service as things

    tag = uuid.uuid4().hex[:6]
    siris = _receipt_setup(monkeypatch, {f"Order {tag}": {"shop": "JB Hi-Fi", "date": "2026-10-20", "items": [{"name": f"Soundbar {tag}", "brand": "Sonos", "price": 899, "warranty_months": 24}]}})
    mail = ReceiptMail([{"uid": "1", "from": "JB Hi-Fi <orders@jbhifi.com.au>", "subject": f"Order {tag} receipt", "date": "Wed, 21 Oct 2026 10:00:00 +0000", "body": "Thanks for your order"}])
    now = datetime(2026, 11, 6, 9, tzinfo=UTC)

    result = asyncio.run(from_email.scan(siris, client=mail, now=now))
    assert result["added"] == 1 and result["more"] == 0
    thing = things.find(siris, f"Soundbar {tag}")[0]
    assert thing["shop"] == "JB Hi-Fi" and thing["bought"] == "2026-10-20" and thing["price"] == 899
    assert thing["warranty_until"] == "2028-10-20" and tag in thing["notes"]

    assert asyncio.run(from_email.scan(siris, client=mail, now=now))["added"] == 0
    assert mail.reads == ["1"], "each email is read once"
    things.delete(siris, thing["id"])
    assert asyncio.run(from_email.scan(siris, client=mail, now=now))["added"] == 0, "one you deleted doesn't come back"


def test_parcels_refunds_and_food_arent_read() -> None:
    from app.things import from_email

    assert not from_email.candidate({"from": "shop@x.com", "subject": "Your order has shipped"})
    assert not from_email.candidate({"from": "shop@x.com", "subject": "Refund for your purchase"})
    assert not from_email.candidate({"from": "Uber Receipts <noreply@uber.com>", "subject": "Your receipt"})
    assert from_email.candidate({"from": "Bunnings <orders@bunnings.com.au>", "subject": "Your tax invoice"})


def test_an_order_and_its_invoice_add_it_once(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.things import from_email, service as things

    tag = uuid.uuid4().hex[:6]
    answer = {"shop": "Bunnings", "date": "2026-10-20", "items": [{"name": f"Drill {tag}"}]}
    siris = _receipt_setup(monkeypatch, {tag: answer})
    mail = ReceiptMail([
        {"uid": "1", "from": "orders@bunnings.com.au", "subject": f"Your order {tag}", "date": "Tue, 20 Oct 2026 10:00:00 +0000", "body": "x"},
        {"uid": "2", "from": "orders@bunnings.com.au", "subject": f"Tax invoice {tag}", "date": "Wed, 21 Oct 2026 10:00:00 +0000", "body": "x"},
    ])
    assert asyncio.run(from_email.scan(siris, client=mail))["added"] == 1
    found = things.find(siris, f"Drill {tag}")
    assert len(found) == 1
    things.delete(siris, found[0]["id"])  # the database is shared with other tests


def test_when_the_model_is_down_the_email_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.llm import local
    from app.things import from_email

    siris = SirisAI(settings=Settings(timezone="UTC"))
    tag = uuid.uuid4().hex[:6]
    mail = ReceiptMail([{"uid": "1", "from": "a@b.com", "subject": f"Receipt {tag}", "date": "Tue, 20 Oct 2026 10:00:00 +0000", "body": "x"}])
    monkeypatch.setattr(local, "client", lambda s: object())

    async def nothing(*a: Any, **k: Any) -> None:
        return None

    monkeypatch.setattr(local, "ask_json", nothing)
    result = asyncio.run(from_email.scan(siris, client=mail))
    assert result["problem"] and result["more"] == 1
    assert from_email._key(mail.messages[0]) not in (siris.state.get(from_email.SEEN_KEY) or [])


def test_a_refused_login_is_said_plainly_and_status_hides_the_password(monkeypatch: pytest.MonkeyPatch) -> None:
    import imaplib

    from app.things import from_email

    siris = _receipt_setup(monkeypatch, {})

    class Refused(ReceiptMail):
        def find_everywhere(self, *a: Any, **k: Any) -> list[dict]:
            raise imaplib.IMAP4.error("AUTHENTICATIONFAILED")

    assert "app password" in asyncio.run(from_email.scan(siris, client=Refused([])))["skipped"]
    assert from_email.status(siris)["connected"] is False
    assert asyncio.run(from_email.scan(siris))["skipped"] == "email isn't set up"
