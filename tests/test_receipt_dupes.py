"""4.33.0: one receipt per purchase, and it's the actual receipt."""

import uuid
from datetime import datetime
from typing import Any

import pytest

from app.core.config import Settings
from app.core.siris import SirisAI
from app.db.models import Thing
from app.documents import receipt_dupes, service as documents


@pytest.fixture(autouse=True)
def _files(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    monkeypatch.setattr(documents, "files_dir", lambda siris, doc_id=None: tmp_path / "receipts" / str(doc_id) if doc_id is not None else tmp_path / "receipts")


def _keep(siris: SirisAI, shop: str, title: str, day: int, total: float | None = None, text: str = "", pdf: bool = False) -> int:
    summary = f"{shop}, {day} Nov 2026" + (f", ${total:,.2f}" if total is not None else "")
    files = [{"filename": "Tax invoice.pdf", "data": b"%PDF"}] if pdf else []
    doc = documents.keep_receipt(siris, title=title, sender=shop, summary=summary, text=text or title, when=datetime(2026, 11, day),
                                 html="<p>x</p>", files=files, receipt="Tax invoice.pdf" if pdf else None)
    return doc["id"]


def _receipts(siris: SirisAI, shop: str) -> list[str]:
    return sorted(d["title"] for d in documents.list_documents(siris, query=shop, kind="receipt", limit=100) if d["sender"] == shop)


def test_an_order_its_invoice_and_its_shipping_email_are_one_receipt_the_invoice() -> None:
    siris = SirisAI(settings=Settings())
    shop = f"Shop{uuid.uuid4().hex[:6]}"
    order = _keep(siris, shop, f"{shop}: Order Confirmation", 5, 587.0, "Thanks for your order. Order number AB12345")
    invoice = _keep(siris, shop, f"{shop}: Tax Invoice", 5, 587.0, "Tax invoice for order AB12345", pdf=True)
    _keep(siris, shop, f"{shop}: Order Shipped", 7, None, "Your order AB12345 is on its way")
    with siris.conversation._session_factory() as session:
        session.add(Thing(name=f"Hoodie {shop}", document_id=order))
        session.commit()
    plan = receipt_dupes.plan(siris, {shop})
    assert len(plan) == 1 and plan[0]["keep"]["id"] == invoice and len(plan[0]["drop"]) == 2
    done = receipt_dupes.dedupe(siris, {shop})
    assert done["set_aside"] == 2 and _receipts(siris, shop) == [f"{shop}: Tax Invoice"]
    with siris.conversation._session_factory() as session:
        thing = session.query(Thing).filter(Thing.name == f"Hoodie {shop}").one()
        assert thing.document_id == invoice                         # the thing now points at the receipt kept
        session.delete(thing)                                       # the database is shared with other tests
        session.commit()
    # Not deleted: not among the receipts, nor the other documents, and they come back.
    assert all(d["kind"] != receipt_dupes.DUPLICATE for d in documents.list_documents(siris, not_kind="receipt", limit=1000))
    assert receipt_dupes.restore(siris) >= 2 and len(_receipts(siris, shop)) == 3


def test_different_purchases_at_one_shop_stay_apart() -> None:
    siris = SirisAI(settings=Settings())
    shop = f"Steam{uuid.uuid4().hex[:6]}"
    _keep(siris, shop, "Steam purchase", 4)                         # no totals, a week apart: two games
    _keep(siris, shop, "Steam purchase", 11)
    _keep(siris, shop, "Gift card", 12, 150.0)                      # different totals
    _keep(siris, shop, "Apparel", 13, 199.0)
    _keep(siris, shop, "Order 98765 receipt", 20, 50.0, "Order number 98765")   # same total, different order numbers
    _keep(siris, shop, "Order 98766 receipt", 21, 50.0, "Order number 98766")
    assert receipt_dupes.plan(siris, {shop}) == []


def test_the_same_receipt_twice_and_one_order_number_across_days() -> None:
    siris = SirisAI(settings=Settings())
    shop = f"Myer{uuid.uuid4().hex[:6]}"
    for day, title in ((3, "Pyjama Set"), (3, "Pyjama Set Order Confirmation"), (4, "Myer: Pyjama Set"), (4, "Myer: pyjama set")):
        _keep(siris, shop, title, day, 59.96)
    gift = f"Harvey{uuid.uuid4().hex[:6]}"
    _keep(siris, gift, "Order Confirmation", 1, 50.0, "Order number 31004333321")
    _keep(siris, gift, "PlayStation Gift Card", 28, 50.0, "Your order 31004333321")   # weeks later, the same order
    plan = receipt_dupes.plan(siris, {shop, gift})
    assert sorted(len(p["drop"]) for p in plan) == [1, 3]
    assert next(p for p in plan if len(p["drop"]) == 3)["keep"]["title"] == "Pyjama Set Order Confirmation"
