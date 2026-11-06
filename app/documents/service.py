"""The document inbox (SirisAI 4.6): a photo or PDF of a letter, bill or
form goes in, and SirisAI reads it, files it in Siris, and reminds you of
anything due.

- **Reading**: a PDF's own text (pypdf); a photo through the vision model
  (SIRISAI_VISION_MODEL, on this machine); a text file as it is.
- **Understanding**: the model on this machine (never a cloud one: letters
  are private) gives it a title, who it's from, what kind it is, a one-line
  summary and anything due, with the date and amount. With no local model,
  dates and amounts are picked out of the text directly.
- **Filing**: a note in Siris (the second brain) with the summary, the
  dates, the full text and the file itself embedded.
- **Reminding**: a reminder SIRISAI_DOCUMENT_REMIND_DAYS (3) days before
  each due date at 9am, and one on the day.

POST /siris/documents takes the file; GET /siris/documents lists them; the
documents_search tool answers "what did the council letter say".
"""

from __future__ import annotations

import io
import json
import logging
import re
from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import or_, select

from app.core import clock
from app.core.user_context import get_current_user_id
from app.db.models import Document
from app.llm import local

if TYPE_CHECKING:
    from app.core.siris import SirisAI

logger = logging.getLogger(__name__)

MAX_BYTES = 20 * 1024 * 1024
KINDS = ("bill", "letter", "form", "receipt", "statement", "notice", "other")
_TRANSCRIBE = (
    "Transcribe all the text in this document exactly, top to bottom, keeping dates, amounts and reference "
    "numbers as written. Reply with the text only."
)
_UNDERSTAND = (
    "You read a letter, bill or form for the person it was sent to. Reply with JSON only: "
    '{"title": "<short title, e.g. \'Council rates notice Q2\'>", "sender": "<who sent it>", '
    f'"kind": "<one of {", ".join(KINDS)}>", "summary": "<one or two plain sentences: what it is and what to do>", '
    '"due": [{"date": "YYYY-MM-DD", "what": "<what is due>", "amount": <number or null>}]}. '
    "Only list dates that something is due by (a payment, a reply, an appointment). Don't invent anything."
)
_MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"], 1
)}
_DATE_WORDS = re.compile(
    r"\b(?:due|by|before|pay by|payment due|closes|expires|appointment|on)\b[^.\n]{0,25}?"
    r"(\d{1,2})(?:st|nd|rd|th)?[ /.-]+([a-z]{3,9}|\d{1,2})[ /.-]+(\d{2,4})",
    re.IGNORECASE,
)
_AMOUNT = re.compile(r"(?:\$|AUD\s?|A\$)\s?(\d{1,3}(?:,\d{3})*(?:\.\d{2})?|\d+(?:\.\d{2})?)")


class DocumentError(ValueError):
    pass


def read_text(siris: "SirisAI", filename: str, content_type: str, data: bytes) -> str:
    kind = (content_type or "").lower()
    name = filename.lower()
    if kind == "application/pdf" or name.endswith(".pdf"):
        from pypdf import PdfReader

        try:
            reader = PdfReader(io.BytesIO(data))
            text = "\n".join((page.extract_text() or "") for page in reader.pages[:30]).strip()
        except Exception as exc:  # noqa: BLE001 - a broken PDF is the user's to know about
            raise DocumentError(f"Couldn't read that PDF ({type(exc).__name__}).") from exc
        if not text:
            raise DocumentError("That PDF has no text in it (a scan?). Send a photo of it instead.")
        return text
    if kind.startswith("image/") or name.endswith((".jpg", ".jpeg", ".png", ".heic", ".webp")):
        if siris.vision is None:
            raise DocumentError("Reading photos needs a vision model (SIRISAI_VISION_MODEL).")
        return siris.vision.describe([data], _TRANSCRIBE)
    if kind.startswith("text/") or name.endswith((".txt", ".md")):
        return data.decode("utf-8", errors="replace")
    raise DocumentError("Send a PDF, a photo (JPEG or PNG) or a text file.")


def _parse_date(day: str, month: str, year: str) -> date | None:
    try:
        m = int(month) if month.isdigit() else _MONTHS.get(month.lower()) or next(
            (v for k, v in _MONTHS.items() if k.startswith(month.lower()[:3])), None
        )
        if m is None:
            return None
        y = int(year)
        if y < 100:
            y += 2000
        return date(y, m, int(day))
    except ValueError:
        return None


def find_due(text: str) -> list[dict[str, Any]]:
    """Due dates and the amount nearest each, without a model (Australian
    day-first dates: "due 14 November 2026", "pay by 14/11/26")."""
    out = []
    for match in _DATE_WORDS.finditer(text):
        found = _parse_date(*match.groups())
        if found is None:
            continue
        nearby = text[max(0, match.start() - 120): match.end() + 120]
        amount = _AMOUNT.search(nearby)
        out.append({
            "date": found.isoformat(),
            "what": " ".join(match.group(0).split())[:60],
            "amount": float(amount.group(1).replace(",", "")) if amount else None,
        })
    seen, unique = set(), []
    for d in out:
        if d["date"] not in seen:
            seen.add(d["date"])
            unique.append(d)
    return unique[:5]


async def understand(siris: "SirisAI", text: str, filename: str) -> dict[str, Any]:
    base = {
        "title": re.sub(r"[_-]+", " ", filename.rsplit(".", 1)[0]).strip() or "Document",
        "sender": "", "kind": "letter", "summary": " ".join(text.split())[:200], "due": find_due(text),
    }
    answer = await local.ask_json(siris, _UNDERSTAND, text[:8000])
    if not answer:
        return base
    due = []
    for d in answer.get("due") or []:
        try:
            due.append({"date": date.fromisoformat(str(d.get("date"))[:10]).isoformat(), "what": str(d.get("what") or "")[:120],
                        "amount": float(d["amount"]) if d.get("amount") not in (None, "") else None})
        except (TypeError, ValueError, AttributeError):
            continue
    kind = str(answer.get("kind") or "letter").lower()
    return {
        "title": str(answer.get("title") or base["title"])[:120],
        "sender": str(answer.get("sender") or "")[:120],
        "kind": kind if kind in KINDS else "other",
        "summary": str(answer.get("summary") or base["summary"])[:600],
        "due": due or base["due"],
    }


def _remind(siris: "SirisAI", doc: dict[str, Any], today: date) -> list[dict[str, Any]]:
    zone = clock.get_zone(siris.settings.timezone)
    ahead = siris.settings.document_remind_days
    made = []
    for d in doc["due"]:
        when = date.fromisoformat(d["date"])
        amount = f" (${d['amount']:,.2f})" if d.get("amount") else ""
        for day, words in ((when - timedelta(days=ahead), f"in {ahead} days"), (when, "today")):
            if ahead <= 0 and words != "today" or day < today:
                continue
            at = datetime.combine(day, time(9), tzinfo=zone)
            made.append(siris.reminders.create(f"{doc['title']}: {d['what'] or 'due'} {words}{amount}", at.astimezone(UTC)))
    return made


def _note(siris: "SirisAI", doc: dict[str, Any], text: str, embed: str | None) -> str | None:
    brain = getattr(siris, "second_brain", None)
    if brain is None:
        return None
    lines = [f"*{doc['kind'].title()}{' from ' + doc['sender'] if doc['sender'] else ''}, added {date.today().isoformat()}.*", "", doc["summary"]]
    if doc["due"]:
        lines += ["", "**Due**"] + [
            f"- {d['date']}: {d['what'] or 'due'}" + (f" (${d['amount']:,.2f})" if d.get("amount") else "") for d in doc["due"]
        ]
    if embed:
        lines += ["", f"![[{embed}]]"]
    lines += ["", "## Text", "", text[:20000]]
    try:
        result = brain.learn([{"title": doc["title"], "content": "\n".join(lines), "tags": ["document", doc["kind"]],
                               "source": "Document inbox"}])
        return (result[0] or {}).get("title") if result else doc["title"]
    except Exception:  # noqa: BLE001 - the document is still kept here
        logger.exception("Filing a document in Siris failed")
        return None


async def ingest(siris: "SirisAI", filename: str, content_type: str, data: bytes, now: datetime | None = None) -> dict[str, Any]:
    if not data:
        raise DocumentError("The file is empty.")
    if len(data) > MAX_BYTES:
        raise DocumentError("That file is over 20 MB.")
    import asyncio

    text = await asyncio.to_thread(read_text, siris, filename, content_type, data)
    doc = await understand(siris, text, filename)
    today = clock.now_local(siris.settings.timezone).date() if now is None else now.astimezone(clock.get_zone(siris.settings.timezone)).date()
    embed = None
    brain = getattr(siris, "second_brain", None)
    if brain is not None:
        try:
            embed = brain.save_attachment(f"{today.isoformat()} {filename}", data)
        except Exception:  # noqa: BLE001
            logger.exception("Saving a document into Siris failed")
    note_title = await asyncio.to_thread(_note, siris, doc, text, embed)
    reminders = _remind(siris, doc, today)
    with siris.conversation._session_factory() as session:
        row = Document(
            user_id=get_current_user_id(), filename=filename[:200], title=doc["title"], kind=doc["kind"], sender=doc["sender"],
            summary=doc["summary"], text=text[:100000], due=json.dumps(doc["due"]), note_title=note_title,
        )
        session.add(row)
        session.commit()
        doc_id = row.id
    if doc["due"]:
        first = doc["due"][0]
        siris.attention.raise_item(
            f"document:{doc_id}", "info", f"Filed: {doc['title']}", source="documents", severity="info",
            body=f"{doc['summary']} Due {first['date']}" + (f" (${first['amount']:,.2f})" if first.get("amount") else "")
            + f". {len(reminders)} reminder{'s' if len(reminders) != 1 else ''} set.",
        )
    return {**get(siris, doc_id), "reminders": len(reminders)}


def _serialize(row: Document, full: bool = False) -> dict[str, Any]:
    out = {
        "id": row.id, "added_at": row.added_at.isoformat(), "filename": row.filename, "title": row.title, "kind": row.kind,
        "sender": row.sender, "summary": row.summary, "due": json.loads(row.due or "[]"), "note_title": row.note_title,
    }
    if full:
        out["text"] = row.text
    return out


def get(siris: "SirisAI", doc_id: int) -> dict[str, Any]:
    with siris.conversation._session_factory() as session:
        row = session.get(Document, doc_id)
        if row is None or row.user_id != get_current_user_id():
            raise KeyError(doc_id)
        return _serialize(row, full=True)


def list_documents(siris: "SirisAI", query: str = "", limit: int = 30) -> list[dict[str, Any]]:
    with siris.conversation._session_factory() as session:
        stmt = select(Document).where(Document.user_id == get_current_user_id())
        for word in [w for w in query.lower().split() if len(w) > 1][:5]:
            like = f"%{word}%"
            stmt = stmt.where(or_(Document.title.ilike(like), Document.sender.ilike(like), Document.summary.ilike(like), Document.text.ilike(like)))
        return [_serialize(r) for r in session.scalars(stmt.order_by(Document.added_at.desc()).limit(limit))]


def delete(siris: "SirisAI", doc_id: int) -> None:
    with siris.conversation._session_factory() as session:
        row = session.get(Document, doc_id)
        if row is None or row.user_id != get_current_user_id():
            raise KeyError(doc_id)
        session.delete(row)
        session.commit()
    siris.attention.resolve_key(f"document:{doc_id}", "Document removed")
