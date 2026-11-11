"""The nightly email import (SIRISAI_GMAIL_HOUR_LOCAL, before the 2:00 upkeep): every email Brad
gives the Gmail label "Siris" (SIRISAI_GMAIL_LABEL) comes into Siris' inbox, so the upkeep's sort
files it as a new note or merges it into the note on the same topic.

- Read-only over IMAP with a Gmail app password (SIRISAI_GMAIL_ADDRESS, SIRISAI_GMAIL_APP_PASSWORD):
  the label is opened read-only, so nothing in Gmail changes, not even "read" flags.
- The note is the email as written: subject as the title, who sent it and when, the body as
  Markdown (HTML mail is converted; quoted replies below "On ... wrote:" are left out).
- Pictures and PDFs attached are saved in the vault's 03-Resources/Attachments/Email and embedded.
- At most SIRISAI_GMAIL_MAX_MESSAGES a night, oldest first; the rest come the next night.

The last message imported (its IMAP UID, with the label's UIDVALIDITY) is kept in the state store.
"""

from __future__ import annotations

import email
import email.policy
import imaplib
import logging
import re
from datetime import datetime
from email.message import EmailMessage
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from app.gmail.markdown import html_to_markdown

if TYPE_CHECKING:
    from app.core.siris import SirisAI

logger = logging.getLogger(__name__)

HOST = "imap.gmail.com"
STATE_KEY = "gmail_import"            # {uidvalidity, last_uid}
LAST_RUN_KEY = "gmail_last_run"
AUTH_ALERT_KEY = "gmail_auth_alerted"
ATTACHMENTS = "03-Resources/Attachments/Email"
KEEP_TYPES = ("image/", "application/pdf")
MAX_ATTACHMENT = 15 * 1024 * 1024


class GmailAuthError(RuntimeError):
    """Gmail refused the address and app password."""


def _safe(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|#^\[\]]+', " ", name).strip()[:80] or "Untitled"


def _title(subject: str) -> str:
    subject = re.sub(r"^\s*((re|fw|fwd)\s*:\s*)+", "", subject or "", flags=re.I).strip()
    return _safe(subject) if subject else ""


def _strip_quoted(text: str) -> str:
    """The new part of a reply: everything above "On <date>, <someone> wrote:" or a forwarded header
    is kept; a forwarded email (the reason for labelling it) is kept whole."""
    if re.search(r"^-+ ?Forwarded message ?-+", text, flags=re.M | re.I):
        return text.strip()
    cut = re.search(r"^(On .{5,200} wrote:|>.*)$", text, flags=re.M)
    return (text[:cut.start()] if cut else text).strip()


def message_to_note(msg: EmailMessage, save: Callable[[str, bytes], str]) -> dict[str, str]:
    """{title, content, source} for one email. `save(name, data)` stores an attachment and returns
    its embed."""
    sender_name, sender = parseaddr(str(msg.get("From", "")))
    try:
        when = parsedate_to_datetime(str(msg.get("Date"))).date().isoformat()
    except (TypeError, ValueError):
        when = ""
    subject = str(msg.get("Subject", "") or "")
    plain = msg.get_body(preferencelist=("plain",))
    html = msg.get_body(preferencelist=("html",))
    if plain is not None:
        body = plain.get_content()
    elif html is not None:
        body = html_to_markdown(html.get_content())[1]
    else:
        body = ""
    body = _strip_quoted(body.replace("\r\n", "\n"))
    title = _title(subject) or _safe(" ".join(body.split()[:8])) or f"Email {when}"
    embeds = []
    for part in msg.iter_attachments():
        kind = part.get_content_type()
        data = part.get_payload(decode=True) or b""
        if not kind.startswith(KEEP_TYPES) or not data or len(data) > MAX_ATTACHMENT:
            continue
        name = _safe(part.get_filename() or f"attachment.{kind.split('/')[-1]}")
        embeds.append(save(f"{when} {_safe(title)[:50]} - {name}".strip(), data))
    who = sender_name or sender
    header = f"*Emailed to Siris: from {who}{' (' + sender + ')' if sender_name and sender else ''}" \
             f"{', ' + when if when else ''}.*"
    parts = [header]
    if body:
        parts.append(body)
    if embeds:
        parts.append("\n".join(f"![[{e}]]" for e in embeds))
    return {"title": title, "content": "\n\n".join(parts), "source": f"Email from {who}" + (f", {when}" if when else "")}


def _push(siris: "SirisAI", message: str, title: str, priority: str | None = None) -> None:
    from app.second_brain.jobs import _push as push
    push(siris, message, title, priority)


def run_import(siris: "SirisAI", now: datetime, connect: Callable[[], imaplib.IMAP4] | None = None) -> dict[str, Any]:
    s = siris.settings
    if not (s.gmail_address and s.gmail_app_password):
        return {"skipped": "Gmail isn't set up"}
    connect = connect or (lambda: imaplib.IMAP4_SSL(HOST, timeout=60))
    try:
        result = _run(siris, connect)
        siris.state.set(AUTH_ALERT_KEY, False)
    except GmailAuthError as exc:
        if not siris.state.get(AUTH_ALERT_KEY):
            _push(siris, "Siris can't read your \"Siris\" emails: Gmail refused the app password. Make a new one at "
                         "myaccount.google.com/apppasswords and put it in SIRISAI_GMAIL_APP_PASSWORD.",
                  "Siris: Gmail sign-in needed", "high")
            siris.state.set(AUTH_ALERT_KEY, True)
        result = {"error": f"Gmail sign-in: {exc}"[:200]}
    from app.core import clock
    siris.state.set(LAST_RUN_KEY, {"date": now.astimezone(clock.get_zone(s.timezone)).date().isoformat(), **result})
    return result


def _run(siris: "SirisAI", connect: Callable[[], imaplib.IMAP4]) -> dict[str, Any]:
    s, brain = siris.settings, siris.second_brain
    vault = Path(brain.path)
    imap = connect()
    try:
        try:
            imap.login(s.gmail_address, s.gmail_app_password)
        except imaplib.IMAP4.error as exc:
            raise GmailAuthError(str(exc)) from exc
        status, _ = imap.select(f'"{s.gmail_label}"', readonly=True)
        if status != "OK":
            return {"skipped": f"no Gmail label called {s.gmail_label!r} yet"}
        validity = (imap.response("UIDVALIDITY")[1] or [b""])[0]
        validity = validity.decode() if isinstance(validity, bytes) else str(validity or "")
        state = dict(siris.state.get(STATE_KEY) or {})
        last = int(state.get("last_uid", 0)) if state.get("uidvalidity") == validity else 0
        status, data = imap.uid("SEARCH", None, f"UID {last + 1}:*")
        uids = sorted(int(u) for u in (data[0] or b"").split() if int(u) > last) if status == "OK" else []
        out: dict[str, Any] = {"imported": [], "waiting": max(0, len(uids) - s.gmail_max_messages)}
        for uid in uids[:s.gmail_max_messages]:
            status, parts = imap.uid("FETCH", str(uid), "(BODY.PEEK[])")
            raw = next((p[1] for p in parts or [] if isinstance(p, tuple)), None)
            if status != "OK" or raw is None:
                break   # try this one again tomorrow

            def save(name: str, blob: bytes) -> str:
                path = vault / ATTACHMENTS / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(blob)
                return path.name

            try:
                msg = email.message_from_bytes(raw, policy=email.policy.default)
                item = message_to_note(msg, save)
                result = brain.learn([{**item, "tags": ["email"], "inbox_if_new": True}])[0]
                out["imported"].append(result.get("title", item["title"]))
            except Exception as exc:  # noqa: BLE001 - one bad email never stops the rest
                logger.warning("Email %s couldn't be imported: %s", uid, exc)
                out.setdefault("failed", []).append(uid)
            siris.state.set(STATE_KEY, {"uidvalidity": validity, "last_uid": uid})
        return out
    finally:
        try:
            imap.logout()
        except Exception:  # noqa: BLE001
            pass
