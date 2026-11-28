"""One receipt per purchase (4.33.0).

A shop often sends several emails about one purchase: the order confirmation, a tax invoice, "your order has
shipped", "it's been delivered", sometimes the same receipt twice. Each was kept as a receipt, so a purchase showed
up three or four times. Now they're grouped by purchase, and only the best of each group stays a receipt:

- **The same purchase**: the same shop and either the same order number, or the same total within
  SAME_TOTAL_DAYS days (and no different order number). A shipping or delivery notice with no total joins the
  shop's one purchase in the SHIPPING_DAYS days before it; any other email with no total only joins a purchase that
  same day, so two Steam games a week apart stay two receipts.
- **The best one**: the actual receipt. A PDF invoice or receipt attached beats an email that has the receipt
  written in it, which beats an order confirmation, which beats a shipping or delivery notice. A total and the
  words "tax invoice" or "receipt" count for it; being a shipping notice counts against it. On a tie, the earliest.

The others aren't deleted: their kind becomes "receipt-duplicate" (so no receipts list shows them), the things
linked to them move to the one kept, and `restore` puts any back.
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from app.core.user_context import get_current_user_id
from app.db.models import Document, Thing

if TYPE_CHECKING:
    from app.core.siris import SirisAI

DUPLICATE = "receipt-duplicate"
STATE_KEY = "receipt_duplicates"  # {duplicate id: id kept}
SAME_TOTAL_DAYS = 10
SHIPPING_DAYS = 30

_TOTAL = re.compile(r"\$\s?([\d,]{1,9}(?:\.\d\d)?)\s*$")
_ORDER = re.compile(r"(?:order|invoice|receipt|transaction|reference|ref)\s*(?:number|no\.?|num|#|id)?\s*[:#]?\s*#?\s*([A-Z0-9][A-Z0-9-]{4,24})", re.I)
_SHIPPING = re.compile(r"\b(shipped|dispatched|on (?:its|the) way|out for delivery|delivered|tracking|ready (?:for|to) (?:pick ?up|collect)|in transit|has been sent|is coming)\b", re.I)
_RECEIPT = re.compile(r"\b(tax invoice|receipt|invoice)\b", re.I)
_CONFIRMATION = re.compile(r"\b(order confirm|thanks for your order|thank you for your order|order received|we.ve got your order)", re.I)
_SHOP_NOISE = re.compile(r"\b(pty|ltd|limited|australia|au|online|store|shop|official|the|inc|co)\b")


def _shop(sender: str) -> str:
    return " ".join(_SHOP_NOISE.sub(" ", re.sub(r"[^a-z0-9 ]+", " ", (sender or "").lower())).split())


def _total(summary: str) -> float | None:
    m = _TOTAL.search(summary or "")
    if not m:
        return None
    try:
        return round(float(m.group(1).replace(",", "")), 2)
    except ValueError:
        return None


def _orders(doc: dict[str, Any]) -> set[str]:
    """Order/invoice numbers in the subject and the first part of the email (with a digit in them)."""
    found = set()
    for m in _ORDER.finditer(f"{doc['title']}\n{(doc.get('text') or '')[:4000]}"):
        token = m.group(1).upper().strip("-")
        if any(ch.isdigit() for ch in token) and not re.fullmatch(r"\d{1,4}", token):
            found.add(token)
    return found


def score(doc: dict[str, Any]) -> int:
    """How much this email is the receipt itself."""
    title, text = doc["title"] or "", (doc.get("text") or "")[:6000]
    s = 0
    if (doc.get("receipt_file") or "").lower().endswith(".pdf"):
        s += 5
    elif doc.get("receipt_file") and not str(doc["receipt_file"]).startswith("email."):
        s += 3  # a photo of the receipt
    if _RECEIPT.search(title) or re.search(r"\btax invoice\b", text, re.I):
        s += 3
    if doc.get("total") is not None:
        s += 2
    if _CONFIRMATION.search(title):
        s += 1
    if _SHIPPING.search(title):
        s -= 4
    return s


def _docs(siris: "SirisAI", shops: set[str] | None = None) -> list[dict[str, Any]]:
    from app.documents import service

    with siris.conversation._session_factory() as session:
        rows = session.scalars(select(Document).where(Document.user_id == get_current_user_id(), Document.kind == "receipt")).all()
        out = [{"id": r.id, "title": r.title, "sender": r.sender, "summary": r.summary, "text": r.text, "added_at": r.added_at} for r in rows]
    if shops is not None:
        wanted = {_shop(s) for s in shops}
        out = [d for d in out if _shop(d["sender"]) in wanted]
    for d in out:
        d["shop"], d["total"], d["receipt_file"] = _shop(d["sender"]), _total(d["summary"]), service.receipt_file(siris, d["id"])
        d["orders"] = _orders(d)
    return out


def groups(docs: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Each purchase's emails (only groups of two or more)."""
    by_shop: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for d in docs:
        if d["shop"]:
            by_shop[d["shop"]].append(d)
    out: list[list[dict[str, Any]]] = []
    for items in by_shop.values():
        items.sort(key=lambda d: d["added_at"])
        purchases: list[list[dict[str, Any]]] = []

        def conflict(a: dict[str, Any], group: list[dict[str, Any]]) -> bool:
            theirs = set().union(*(g["orders"] for g in group))
            return bool(a["orders"] and theirs and not (a["orders"] & theirs))

        for d in items:
            home = None
            for g in purchases:  # the same order number
                if d["orders"] and any(d["orders"] & g_doc["orders"] for g_doc in g):
                    home = g
                    break
            if home is None and d["total"] is not None:  # the same total, close together
                for g in reversed(purchases):
                    if any(x["total"] == d["total"] and abs((d["added_at"] - x["added_at"]).days) <= SAME_TOTAL_DAYS for x in g) and not conflict(d, g):
                        home = g
                        break
            if home is None and d["total"] is None:
                # A shipping or delivery notice joins the one purchase just before it; any other email without a total
                # only the one purchase that same day (two Steam games a week apart are two purchases).
                window = SHIPPING_DAYS if _SHIPPING.search(d["title"] or "") else 0
                recent = [g for g in purchases if 0 <= (d["added_at"].date() - g[0]["added_at"].date()).days <= window and not conflict(d, g)]
                if len(recent) == 1:
                    home = recent[0]
            if home is None:
                purchases.append([d])
            else:
                home.append(d)
        out += [g for g in purchases if len(g) > 1]
    return out


def best(group: list[dict[str, Any]]) -> dict[str, Any]:
    return max(group, key=lambda d: (score(d), -d["added_at"].timestamp()))


def plan(siris: "SirisAI", shops: set[str] | None = None) -> list[dict[str, Any]]:
    """What `dedupe` would do (all shops, or just `shops`): [{keep, drop: [...]}], changing nothing."""
    out = []
    for g in groups(_docs(siris, shops)):
        keep = best(g)
        out.append({"keep": {"id": keep["id"], "title": keep["title"], "score": score(keep)},
                    "drop": [{"id": d["id"], "title": d["title"], "score": score(d)} for d in g if d is not keep]})
    return out


def dedupe(siris: "SirisAI", shops: set[str] | None = None) -> dict[str, Any]:
    """Keep the best receipt of each purchase; set the others aside (kind receipt-duplicate) and move their things."""
    done = plan(siris, shops)
    mapping = dict(siris.state.get(STATE_KEY) or {})
    with siris.conversation._session_factory() as session:
        for p in done:
            keep_id = p["keep"]["id"]
            for d in p["drop"]:
                row = session.get(Document, d["id"])
                if row is None or row.kind != "receipt":
                    continue
                row.kind = DUPLICATE
                mapping[str(d["id"])] = keep_id
                for thing in session.scalars(select(Thing).where(Thing.document_id == d["id"])):
                    thing.document_id = keep_id
        session.commit()
    siris.state.set(STATE_KEY, mapping)
    return {"purchases": len(done), "set_aside": sum(len(p["drop"]) for p in done), "at": datetime.now().isoformat(timespec="seconds"), "plan": done}


def restore(siris: "SirisAI", doc_id: int | None = None) -> int:
    """Put set-aside receipts back (one, or all)."""
    mapping = dict(siris.state.get(STATE_KEY) or {})
    ids = [doc_id] if doc_id else [int(k) for k in mapping]
    back = 0
    with siris.conversation._session_factory() as session:
        for i in ids:
            row = session.get(Document, i)
            if row is not None and row.kind == DUPLICATE:
                row.kind = "receipt"
                back += 1
            mapping.pop(str(i), None)
        session.commit()
    siris.state.set(STATE_KEY, mapping)
    return back
