"""Bills and subscriptions from email (SirisAI 4.6).

Once a day (SIRISAI_BILLS_HOUR_LOCAL, 8am) SirisAI looks through the last
three weeks of email (SIRISAI_EMAIL_*, read-only: nothing is marked read)
for:

- a subscription that **renews** soon, and for how much;
- a free **trial ending** (when it starts charging);
- a **price rise** (from what to what);
- a **bill due** (how much, by when).

Each one is kept (GET /siris/bills), and goes to the attention feed
SIRISAI_BILLS_WARN_DAYS (4) before it's due; a price rise straight away.
"Done" or "Ignore" on the item closes it for good. Receipts for things
already paid, and newsletters, are left alone. Only the email's own words
are used, no model, so it's the same every day.
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import UTC, date, datetime, timedelta
from email.utils import parseaddr, parsedate_to_datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from app.core import clock
from app.db.models import Bill
from app.hub.attention import action, register_action

if TYPE_CHECKING:
    from app.core.siris import SirisAI

logger = logging.getLogger(__name__)

SEARCH_WORDS = ["renew", "subscription", "trial", "invoice", "price", "bill"]
_MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
_AMOUNT = r"(?:A?\$|AUD\s?|USD\s?|US\$|€|£)\s?(\d{1,3}(?:,\d{3})*(?:\.\d{2})?|\d+(?:\.\d{2})?)"
_DATE = (
    r"(\d{1,2})(?:st|nd|rd|th)?\s+(" + "|".join(_MONTHS) + r")[a-z]*\.?,?\s+(\d{4})"
    r"|(" + "|".join(_MONTHS) + r")[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})"
    r"|(\d{1,2})/(\d{1,2})/(\d{2,4})"
    r"|(\d{4})-(\d{2})-(\d{2})"
)
_KINDS = [
    ("price_rise", re.compile(r"price (?:increase|change|rise|update)|(?:increas|chang)\w* (?:the |your )?(?:price|subscription fee)|new price", re.I)),
    ("trial_ending", re.compile(r"trial (?:ends|will end|is ending|expires)|(?:end|expir)\w* of (?:your )?(?:free )?trial|after your (?:free )?trial", re.I)),
    ("renewal", re.compile(r"(?:will|is set to|due to) (?:auto[- ]?)?renew|renews on|renewal (?:date|notice|reminder)|upcoming renewal|subscription (?:will )?renew", re.I)),
    ("bill_due", re.compile(r"(?:payment|amount|bill|invoice|balance) (?:is )?due|due date|pay by|please pay|new (?:bill|invoice|statement)", re.I)),
]
_PAID = re.compile(r"\b(receipt|thank you for your (?:payment|purchase|order)|payment (?:received|successful|confirmed)|has been paid)\b", re.I)
_DUE_NEAR = re.compile(r"(renew\w*|due|ends?|expires?|charged?|starting|from|effective|on|by|before)\W{1,3}(?:on\s+)?$", re.I)


def _date(groups: tuple[str | None, ...]) -> date | None:
    g = groups
    try:
        if g[0]:
            return date(int(g[2]), _MONTHS.index(g[1][:3].lower()) + 1, int(g[0]))
        if g[3]:
            return date(int(g[5]), _MONTHS.index(g[3][:3].lower()) + 1, int(g[4]))
        if g[6]:
            year = int(g[8]) + (2000 if len(g[8]) == 2 else 0)
            return date(year, int(g[7]), int(g[6]))  # day first (Australia)
        if g[9]:
            return date(int(g[9]), int(g[10]), int(g[11]))
    except (ValueError, TypeError):
        return None
    return None


def _service(sender: str, subject: str) -> str:
    name, address = parseaddr(sender)
    if name:
        return re.sub(r"\b(team|billing|no-?reply|support|accounts?)\b", "", name, flags=re.I).strip(" -|,") or name
    domain = address.split("@")[-1].split(".")
    return (domain[-2] if len(domain) >= 2 else domain[0]).title() or subject[:30]


def classify(sender: str, subject: str, body: str) -> dict[str, Any] | None:
    """What kind of bill this email is about, with the amount(s) and date,
    or None when it isn't one (a receipt, a newsletter)."""
    text = f"{subject}\n{body}"
    kind = next((k for k, pattern in _KINDS if pattern.search(text)), None)
    if kind is None or (_PAID.search(text) and kind == "bill_due"):
        return None
    amounts = [float(a.replace(",", "")) for a in re.findall(_AMOUNT, text, re.I)]
    currency = "$"
    found = re.search(r"(€|£|USD|US\$)", text)
    if found:
        currency = found.group(1)
    due = None
    for match in re.finditer(_DATE, text, re.I):
        before = text[max(0, match.start() - 40): match.start()]
        candidate = _date(match.groups())
        if candidate and (_DUE_NEAR.search(before) or due is None):
            due = candidate
            if _DUE_NEAR.search(before):
                break
    out: dict[str, Any] = {"kind": kind, "service": _service(sender, subject), "currency": currency,
                           "due_date": due.isoformat() if due else None, "amount": amounts[0] if amounts else None, "old_amount": None}
    if kind == "price_rise" and len(amounts) >= 2:
        rise = re.search(_AMOUNT + r".{0,40}?\bto\b\s*" + _AMOUNT, text, re.I | re.S)
        if rise:
            out["old_amount"], out["amount"] = float(rise.group(1).replace(",", "")), float(rise.group(2).replace(",", ""))
        else:
            out["old_amount"], out["amount"] = min(amounts[:2]), max(amounts[:2])
    return out


def _key(message: dict[str, Any]) -> str:
    return hashlib.sha256(f"{message.get('from')}|{message.get('subject')}|{message.get('date')}".encode()).hexdigest()[:32]


def scan(siris: "SirisAI", client: Any = None, now: datetime | None = None) -> dict[str, Any]:
    client = client or getattr(siris, "email_client", None)
    if client is None:
        return {"skipped": "email isn't set up (SIRISAI_EMAIL_*)"}
    found = client.recent_about(SEARCH_WORDS, limit=15, since_days=21)
    added = 0
    with siris.conversation._session_factory() as session:
        known = {k for (k,) in session.execute(select(Bill.message_id)).all()}
        for message in found:
            key = _key(message)
            if key in known:
                continue
            try:
                body = client.read(message["uid"]).get("body", "")
            except Exception:  # noqa: BLE001 - the snippet will do
                body = message.get("snippet", "")
            info = classify(message.get("from", ""), message.get("subject", ""), body)
            known.add(key)
            if info is None:
                continue
            try:
                sent = parsedate_to_datetime(message.get("date") or "").astimezone(UTC).replace(tzinfo=None)
            except (TypeError, ValueError):
                sent = None
            session.add(Bill(message_id=key, sent_at=sent, subject=(message.get("subject") or "")[:300], **info))
            added += 1
        session.commit()
    raised = check(siris, now)
    return {"looked_at": len(found), "added": added, "raised": raised}


def _label(bill: dict[str, Any]) -> str:
    money = f"{bill['currency']}{bill['amount']:,.2f}" if bill.get("amount") is not None else ""
    when = date.fromisoformat(bill["due_date"]).strftime("%a %-d %b") if bill.get("due_date") else ""
    if bill["kind"] == "price_rise":
        old = f"{bill['currency']}{bill['old_amount']:,.2f} to " if bill.get("old_amount") is not None else "to "
        return f"{bill['service']} is putting its price up, {old}{money}" + (f" from {when}" if when else "")
    if bill["kind"] == "trial_ending":
        return f"{bill['service']} trial ends" + (f" {when}" if when else "") + (f", then {money}" if money else "")
    if bill["kind"] == "renewal":
        return f"{bill['service']} renews" + (f" {when}" if when else "") + (f" for {money}" if money else "")
    return f"{bill['service']} bill" + (f" of {money}" if money else "") + (f" due {when}" if when else "")


def _serialize(row: Bill) -> dict[str, Any]:
    return {
        "id": row.id, "kind": row.kind, "service": row.service, "subject": row.subject, "amount": row.amount,
        "old_amount": row.old_amount, "currency": row.currency, "due_date": row.due_date, "status": row.status,
        "sent_at": row.sent_at.isoformat() if row.sent_at else None,
    }


def upcoming(siris: "SirisAI", include_closed: bool = False, now: datetime | None = None) -> list[dict[str, Any]]:
    today = (now or datetime.now(UTC)).astimezone(clock.get_zone(siris.settings.timezone)).date()
    with siris.conversation._session_factory() as session:
        stmt = select(Bill)
        if not include_closed:
            stmt = stmt.where(Bill.status == "open")
        rows = [_serialize(r) for r in session.scalars(stmt)]
    rows = [r for r in rows if include_closed or not r["due_date"] or r["due_date"] >= (today - timedelta(days=1)).isoformat()]
    for r in rows:
        r["label"] = _label(r)
    return sorted(rows, key=lambda r: r["due_date"] or "9999")


def check(siris: "SirisAI", now: datetime | None = None) -> int:
    """Raise the ones coming up within SIRISAI_BILLS_WARN_DAYS, and every
    price rise; close the rest."""
    today = (now or datetime.now(UTC)).astimezone(clock.get_zone(siris.settings.timezone)).date()
    horizon = (today + timedelta(days=siris.settings.bills_warn_days)).isoformat()
    wanted = []
    for bill in upcoming(siris, now=now):
        soon = bill["due_date"] is not None and bill["due_date"] <= horizon
        if not (soon or bill["kind"] == "price_rise"):
            continue
        wanted.append({
            "key": f"bill:{bill['id']}", "kind": "suggestion", "title": bill["label"],
            "severity": "attention" if bill["kind"] in ("trial_ending", "bill_due") else "info",
            "body": f"From the email \"{bill['subject']}\". Cancel or switch before then if you don't want it.",
            "actions": [action("done", "Done", "primary"), action("ignore", "Ignore")],
            "payload": {"bill_id": bill["id"]},
        })
    siris.attention.sync("bills", wanted, outcome="No longer coming up")
    return len(wanted)


def set_status(siris: "SirisAI", bill_id: int, status: str) -> dict[str, Any]:
    with siris.conversation._session_factory() as session:
        row = session.get(Bill, bill_id)
        if row is None:
            raise KeyError(bill_id)
        row.status = status
        session.commit()
        return _serialize(row)


def _act(status: str) -> Any:
    def handler(siris: "SirisAI", item: dict[str, Any], payload: dict[str, Any], trigger: str) -> tuple[Any, str]:
        bill_id = int((item.get("payload") or payload or {}).get("bill_id"))
        return set_status(siris, bill_id, status), "Marked done" if status == "done" else "Ignored"

    return handler


register_action("bills", "done", _act("done"))
register_action("bills", "ignore", _act("ignored"))
