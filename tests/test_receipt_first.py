"""5.41: the receipt itself, not the email saying it's attached."""

import asyncio
import uuid
from email.message import EmailMessage
from typing import Any

import pytest

from app.core.config import Settings
from app.core.siris import SirisAI
from app.documents import service as documents
from app.email.client import attachments, filename, inline_images
from app.things import from_email


@pytest.fixture(autouse=True)
def _files(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    monkeypatch.setattr(documents, "files_dir", lambda siris, doc_id=None: tmp_path / "receipts" / str(doc_id) if doc_id is not None else tmp_path / "receipts")


def _email() -> EmailMessage:
    m = EmailMessage()
    m["Subject"] = "Your receipt"
    m.set_content("Please find your invoice attached.")
    m.add_alternative('<p>Please find your invoice attached.</p><img src="cid:logo@x">', subtype="html")
    m.get_payload()[1].add_related(b"\x89PNG" + b"0" * 100, maintype="image", subtype="png", cid="<logo@x>")
    m.add_attachment(b"%PDF-1.4 Tax invoice total $50", maintype="application", subtype="octet-stream", filename="=?utf-8?b?UmVjZWlwdCAxNiBOb3YucGRm?=")
    return m


def test_attachments_are_found_named_and_typed_whatever_the_sender_said() -> None:
    m = _email()
    files = attachments(m)
    assert [(f["filename"], f["type"]) for f in files] == [("Receipt 16 Nov.pdf", "application/pdf")]
    assert [i["cid"] for i in inline_images(m)] == ["logo@x"]
    part = next(p for p in m.walk() if p.get_filename())
    assert filename(part) == "Receipt 16 Nov.pdf"


def test_pictures_in_the_email_are_put_back_in_it() -> None:
    html = '<img src="cid:logo@x"><img src="https://tracker.example/p.gif">'
    out = from_email.embed_inline(html, [{"cid": "logo@x", "data": b"abc", "type": "image/png"}])
    assert 'src="data:image/png;base64,YWJj"' in out and "tracker.example" in out  # the remote one stays blocked by the page's policy


def _pdf(name: str, text: str) -> dict:
    return {"filename": name, "data": b"x", "type": "application/pdf", "text": text}


def test_the_receipt_is_the_invoice_pdf_not_the_terms() -> None:
    siris = SirisAI(settings=Settings(timezone="UTC"))
    mail = {"body": "Please find your invoice attached.", "files": [_pdf("Terms and conditions.pdf", "Returns policy"), _pdf("INV-1.pdf", "Tax invoice. Total $50.00")], "inline": []}
    assert asyncio.run(from_email.choose(siris, mail)) == "INV-1.pdf"


def test_a_receipt_written_in_the_email_is_the_email() -> None:
    siris = SirisAI(settings=Settings(timezone="UTC"))
    body = "Thanks for your order. " + "Item: Speaker $899. " * 80
    mail = {"body": body, "files": [], "inline": [{"cid": "logo", "filename": "logo.png", "data": b"0" * 5000, "type": "image/png"}]}
    assert asyncio.run(from_email.choose(siris, mail)) is None


def test_a_short_email_around_one_big_picture_is_that_picture() -> None:
    siris = SirisAI(settings=Settings(timezone="UTC"))
    mail = {"body": "Your Aquila receipt. Thank you for shopping.", "files": [],
            "inline": [{"cid": "logo", "filename": "logo.png", "data": b"0" * 4000, "type": "image/png"},
                       {"cid": "r", "filename": "d3020.png", "data": b"0" * 90_000, "type": "image/png"}]}
    assert asyncio.run(from_email.choose(siris, mail)) == "d3020.png"


def test_two_likely_invoices_ask_the_model(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.llm import local

    siris = SirisAI(settings=Settings(timezone="UTC"))
    asked = []

    async def answer(siris: Any, instructions: str, text: str, llm: Any = None) -> dict:
        asked.append(text)
        return {"receipt": "Invoice B.pdf"}

    monkeypatch.setattr(local, "ask_json", answer)
    mail = {"body": "Invoices attached", "files": [_pdf("Invoice A.pdf", "Invoice total $10"), _pdf("Invoice B.pdf", "Tax invoice total $20")], "inline": []}
    assert asyncio.run(from_email.choose(siris, mail)) == "Invoice B.pdf"
    assert "Invoice A.pdf" in asked[0]


def test_the_receipt_file_is_marked_and_listed_first() -> None:
    siris = SirisAI(settings=Settings(timezone="UTC"))
    tag = uuid.uuid4().hex[:6]
    from datetime import datetime

    doc = documents.keep_receipt(siris, title=f"Shop {tag}", sender="Shop", summary="", text="t", when=datetime(2026, 1, 1), html="<p>x</p>",
                                 files=[{"filename": "a/b.pdf", "data": b"pdf"}], receipt="a/b.pdf")
    assert doc["receipt_file"] == "a_b.pdf" and doc["files"][0] == "a_b.pdf" and ".receipt" not in doc["files"]
    email_only = documents.keep_receipt(siris, title=f"Shop {tag}", sender="Shop", summary="", text="t", when=datetime(2026, 1, 1), html="<p>x</p>")
    assert email_only["receipt_file"] == "email.html"
    documents.delete(siris, doc["id"])
    documents.delete(siris, email_only["id"])


def test_receipts_kept_before_are_redone_and_removed_ones_stay_removed(monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import datetime

    siris = SirisAI(settings=Settings(timezone="UTC"))
    tag = uuid.uuid4().hex[:6]
    body = f"Invoice {tag} attached"
    pdf = {"filename": "INV.pdf", "data": b"x", "type": "application/pdf"}
    monkeypatch.setattr(documents, "read_text", lambda siris, name, kind, data: f"Tax invoice {tag} total $5")
    text = f"{body}\n\nTax invoice {tag} total $5"
    old = documents.keep_receipt(siris, title="Old", sender="Shop", summary="", text=text, when=datetime(2026, 1, 1), html="<p>x</p>", files=[{"filename": "INV.pdf", "data": b"x"}])
    messages = [{"uid": "1", "from": "s@x.com", "subject": f"Invoice {tag}", "date": "Tue, 20 Oct 2026 10:00:00 +0000"},
                {"uid": "2", "from": "s@x.com", "subject": f"Removed {tag}", "date": "Tue, 20 Oct 2026 10:00:00 +0000"}]
    siris.state.set(from_email.KEPT_KEY, [from_email._key(m) for m in messages])

    class Mail:
        def find_everywhere(self, *a: Any, **k: Any) -> list[dict]:
            return messages

        def read_everywhere(self, uid: str) -> dict:
            return {"body": body if uid == "1" else "something you removed", "html": "<p>x</p>", "files": [dict(pdf)], "inline": []}

    result = asyncio.run(from_email.refresh(siris, client=Mail()))
    assert result["redone"] == 1 and result["not_matched"] == 1
    assert documents.get(siris, old["id"])["receipt_file"] == "INV.pdf"
    assert len(documents.list_documents(siris, kind="receipt", limit=1000)) >= 1
    documents.delete(siris, old["id"])
