"""The things you own (SirisAI 4.9): warranties and manuals.

- **From receipts**: a receipt or invoice added to the document inbox (POST
  /siris/documents) has its items read by the local model: name, brand,
  model, price and warranty. Each becomes a Thing, linked to the receipt.
  You can also add them by hand (POST /siris/things) or from SirisOS.
- **Warranties**: SIRISAI_WARRANTY_WARN_DAYS (30) before one runs out, the
  attention feed says so ("The dishwasher's warranty ends 12 Dec: anything
  to claim for?"). Checked daily.
- **Asking**: things_find answers "where's the dishwasher manual", "when
  did I buy the TV", "is the fridge still under warranty".

A warranty given in years or months is worked out from the purchase date;
Australian Consumer Law gives more than the warranty in many cases, so the
reminder says to check before assuming it's over.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import or_, select

from app.core import clock
from app.core.user_context import get_current_user_id
from app.db.models import Thing

if TYPE_CHECKING:
    from app.core.siris import SirisAI

FIELDS = ("name", "brand", "model", "room", "bought", "price", "shop", "warranty_until", "manual", "document_id", "notes")
RECEIPT_ITEMS = (
    ' Also, if it is a receipt or invoice for things bought, add "items": [{"name": "<what it is, e.g. dishwasher>", '
    '"brand": "", "model": "", "price": <number or null>, "warranty_months": <number or null>}] for each item worth '
    'keeping track of (appliances, electronics, tools, furniture; not food or consumables).'
)


def _months_after(start: date, months: int) -> date:
    year, month = divmod(start.month - 1 + months, 12)
    year += start.year
    month += 1
    for day in (start.day, 30, 29, 28):
        try:
            return date(year, month, day)
        except ValueError:
            continue
    return date(year, month, 28)


def _serialize(row: Thing, today: date | None = None) -> dict[str, Any]:
    out = {k: getattr(row, k) for k in FIELDS}
    out["id"] = row.id
    if row.warranty_until and today:
        left = (date.fromisoformat(row.warranty_until) - today).days
        out["warranty_days_left"] = left
        out["under_warranty"] = left >= 0
    return out


def _today(siris: "SirisAI", now: datetime | None = None) -> date:
    return (now or datetime.now(UTC)).astimezone(clock.get_zone(siris.settings.timezone)).date()


def add(siris: "SirisAI", item: dict[str, Any]) -> dict[str, Any]:
    if not str(item.get("name") or "").strip():
        raise ValueError("A thing needs a name.")
    data = {k: item.get(k) for k in FIELDS if item.get(k) not in (None, "")}
    if item.get("warranty_months") and data.get("bought") and not data.get("warranty_until"):
        data["warranty_until"] = _months_after(date.fromisoformat(str(data["bought"])[:10]), int(item["warranty_months"])).isoformat()
    for key in ("bought", "warranty_until"):
        if data.get(key):
            data[key] = date.fromisoformat(str(data[key])[:10]).isoformat()
    with siris.conversation._session_factory() as session:
        row = Thing(user_id=get_current_user_id(), **data)
        session.add(row)
        session.commit()
        return _serialize(row, _today(siris))


def update(siris: "SirisAI", thing_id: int, changes: dict[str, Any]) -> dict[str, Any]:
    with siris.conversation._session_factory() as session:
        row = session.get(Thing, thing_id)
        if row is None or row.user_id != get_current_user_id():
            raise KeyError(thing_id)
        for key in FIELDS:
            if key in changes:
                value = changes[key]
                if key in ("bought", "warranty_until") and value:
                    value = date.fromisoformat(str(value)[:10]).isoformat()
                setattr(row, key, value)
        session.commit()
        return _serialize(row, _today(siris))


def delete(siris: "SirisAI", thing_id: int) -> None:
    with siris.conversation._session_factory() as session:
        row = session.get(Thing, thing_id)
        if row is None or row.user_id != get_current_user_id():
            raise KeyError(thing_id)
        session.delete(row)
        session.commit()


def find(siris: "SirisAI", query: str = "", limit: int = 50) -> list[dict[str, Any]]:
    today = _today(siris)
    with siris.conversation._session_factory() as session:
        stmt = select(Thing).where(Thing.user_id == get_current_user_id())
        for word in [w for w in query.lower().split() if len(w) > 2 and w not in {"the", "my", "our", "manual", "warranty"}][:4]:
            like = f"%{word.rstrip('s')}%"
            stmt = stmt.where(or_(Thing.name.ilike(like), Thing.brand.ilike(like), Thing.model.ilike(like), Thing.room.ilike(like), Thing.notes.ilike(like)))
        return [_serialize(r, today) for r in session.scalars(stmt.order_by(Thing.name).limit(limit))]


def from_receipt(siris: "SirisAI", items: list[dict[str, Any]], bought: str | None, shop: str, document_id: int) -> list[dict[str, Any]]:
    added = []
    for item in items[:20]:
        if not isinstance(item, dict) or not str(item.get("name") or "").strip():
            continue
        try:
            added.append(add(siris, {
                "name": str(item["name"])[:120], "brand": str(item.get("brand") or "")[:80], "model": str(item.get("model") or "")[:80],
                "price": float(item["price"]) if item.get("price") not in (None, "") else None, "bought": bought, "shop": shop[:120],
                "warranty_months": int(item["warranty_months"]) if item.get("warranty_months") else None, "document_id": document_id,
            }))
        except (TypeError, ValueError):
            continue
    return added


def check(siris: "SirisAI", now: datetime | None = None) -> int:
    """Warranties ending within SIRISAI_WARRANTY_WARN_DAYS."""
    today = _today(siris, now)
    horizon = (today + timedelta(days=siris.settings.warranty_warn_days)).isoformat()
    wanted = []
    with siris.conversation._session_factory() as session:
        rows = list(session.scalars(select(Thing).where(Thing.warranty_until.is_not(None), Thing.warranty_until >= today.isoformat(),
                                                        Thing.warranty_until <= horizon)))
    for row in rows:
        ends = date.fromisoformat(row.warranty_until or today.isoformat())
        wanted.append({
            "key": f"warranty:{row.id}", "kind": "suggestion", "severity": "info",
            "title": f"The {row.name}'s warranty ends {ends.strftime('%a %-d %b')}",
            "body": "Anything wrong with it to claim for first? (Australian Consumer Law can cover it for longer than the "
                    "warranty, so check before assuming it's over.)" + (f" Manual: {row.manual}." if row.manual else ""),
        })
    siris.attention.sync("things", wanted, outcome="Warranty passed")
    return len(wanted)
