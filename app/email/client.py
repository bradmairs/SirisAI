"""Email over plain IMAP + SMTP (stdlib imaplib/smtplib) -- works with
Gmail/Fastmail/iCloud app passwords, a self-hosted Dovecot, anything.

Triage, not a mail client: list unread mail with a cheap importance call,
read one message as text, save a reply as a draft (always safe -- nothing
leaves), or send a reply (needs confirmation; see app/plugins/builtin/email.py).

"Important" is deliberately simple and explainable: a message is low
priority when it looks automated -- a List-Unsubscribe or "Precedence:
bulk/list" header, or a no-reply sender -- and otherwise important, more so
when it's addressed to you directly rather than CC'd.
"""

import email
import email.utils
import imaplib
import re
import smtplib
import time
from email.header import decode_header, make_header
from email.message import EmailMessage, Message
from html.parser import HTMLParser
from typing import Any

_NOREPLY_RE = re.compile(r"no[-_.]?reply|do[-_.]?not[-_.]?reply|notifications?@|mailer-daemon", re.IGNORECASE)


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in ("br", "p", "div", "li", "tr"):
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def _html_to_text(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    return re.sub(r"\n\s*\n+", "\n\n", "".join(parser.parts)).strip()


def _header(message: Message, name: str) -> str:
    value = message.get(name)
    if value is None:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except (ValueError, LookupError):
        return str(value)


def body_text(message: Message) -> str:
    plain, html = None, None
    for part in message.walk() if message.is_multipart() else [message]:
        if part.get_content_maintype() == "multipart" or part.get("Content-Disposition", "").startswith("attachment"):
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        if part.get_content_type() == "text/plain" and plain is None:
            plain = text
        elif part.get_content_type() == "text/html" and html is None:
            html = text
    return (plain if plain is not None else _html_to_text(html or "")).strip()


def classify(message: Message, my_address: str) -> tuple[bool, list[str]]:
    reasons = []
    sender = _header(message, "From")
    if message.get("List-Unsubscribe") or message.get("List-Id"):
        reasons.append("mailing list / newsletter")
    if str(message.get("Precedence", "")).lower() in ("bulk", "list", "junk"):
        reasons.append("bulk mail")
    if _NOREPLY_RE.search(sender):
        reasons.append("automated sender")
    if reasons:
        return False, reasons
    to = " ".join(addr for _, addr in email.utils.getaddresses([_header(message, "To")])).lower()
    return True, ["sent to you directly"] if my_address and my_address.lower() in to else ["from a person"]


class EmailClient:
    def __init__(
        self,
        imap_host: str,
        username: str,
        password: str,
        imap_port: int = 993,
        smtp_host: str = "",
        smtp_port: int = 587,
        from_address: str = "",
        drafts_folder: str = "Drafts",
        use_ssl: bool = True,
        timeout: float = 20.0,
    ) -> None:
        self._imap_host, self._imap_port = imap_host, imap_port
        self._username, self._password = username, password
        self._smtp_host, self._smtp_port = smtp_host, smtp_port
        self.address = from_address or username
        self._drafts = drafts_folder
        self._ssl = use_ssl
        self._timeout = timeout

    def _imap(self) -> imaplib.IMAP4:
        conn = (imaplib.IMAP4_SSL if self._ssl else imaplib.IMAP4)(self._imap_host, self._imap_port, timeout=self._timeout)
        conn.login(self._username, self._password)
        return conn

    def _fetch(self, conn: imaplib.IMAP4, uid: str, peek: bool = True) -> Message:
        status, data = conn.uid("fetch", uid, "(BODY.PEEK[])" if peek else "(RFC822)")
        if status != "OK" or not data or not isinstance(data[0], tuple):
            raise ValueError(f"No message with uid {uid}")
        return email.message_from_bytes(data[0][1])

    def list_unread(self, limit: int = 20, since_days: int = 3) -> list[dict[str, Any]]:
        """Unread messages from the last few days, newest first, without
        marking them read."""
        conn = self._imap()
        try:
            conn.select("INBOX", readonly=True)
            since = time.strftime("%d-%b-%Y", time.gmtime(time.time() - since_days * 86400))
            status, data = conn.uid("search", None, "UNSEEN", "SINCE", since)
            uids = data[0].split()[-limit:] if status == "OK" and data and data[0] else []
            out = []
            for uid in reversed(uids):
                message = self._fetch(conn, uid.decode())
                important, reasons = classify(message, self.address)
                text = body_text(message)
                out.append(
                    {
                        "uid": uid.decode(),
                        "from": _header(message, "From"),
                        "subject": _header(message, "Subject"),
                        "date": _header(message, "Date"),
                        "snippet": re.sub(r"\s+", " ", text)[:200],
                        "important": important,
                        "why": reasons,
                    }
                )
            return out
        finally:
            conn.logout()

    def recent_with(self, addresses: list[str], limit: int = 5, since_days: int = 60) -> list[dict[str, Any]]:
        """The latest messages from any of `addresses` (read or not), newest
        first, without marking anything read: meeting prep in SirisOS."""
        good = [a for a in addresses[:8] if re.fullmatch(r"[^\s\"()]+@[^\s\"()]+", a)]
        return self._recent([("FROM", a) for a in good], limit, since_days)

    def recent_about(self, words: list[str], limit: int = 5, since_days: int = 90) -> list[dict[str, Any]]:
        """The latest messages whose subject has any of `words` (a project's
        name and aliases), newest first, without marking anything read."""
        clean = [w for w in (re.sub(r'["\\\r\n]', " ", w).strip() for w in words[:6]) if len(w) >= 3]
        return self._recent([("SUBJECT", w) for w in clean], limit, since_days)

    def _recent(self, criteria: list[tuple[str, str]], limit: int, since_days: int) -> list[dict[str, Any]]:
        if not criteria:
            return []
        conn = self._imap()
        try:
            conn.select("INBOX", readonly=True)
            since = time.strftime("%d-%b-%Y", time.gmtime(time.time() - since_days * 86400))
            found: dict[str, Message] = {}
            for key, value in criteria:
                status, data = conn.uid("search", None, key, f'"{value}"', "SINCE", since)
                for uid in (data[0].split()[-limit:] if status == "OK" and data and data[0] else []):
                    found.setdefault(uid.decode(), None)  # type: ignore[arg-type]
            newest = sorted(found, key=int)[-limit * 2:]
            out = []
            for uid in newest:
                message = self._fetch(conn, uid)
                out.append({
                    "uid": uid,
                    "from": _header(message, "From"),
                    "subject": _header(message, "Subject"),
                    "date": _header(message, "Date"),
                    "snippet": re.sub(r"\s+", " ", body_text(message))[:240],
                })
        finally:
            conn.logout()

        def when(m: dict[str, Any]) -> float:
            try:
                return email.utils.parsedate_to_datetime(m["date"]).timestamp()
            except (TypeError, ValueError):
                return 0.0

        return sorted(out, key=when, reverse=True)[:limit]

    def read(self, uid: str) -> dict[str, Any]:
        conn = self._imap()
        try:
            conn.select("INBOX", readonly=True)
            message = self._fetch(conn, uid)
        finally:
            conn.logout()
        return {
            "uid": uid,
            "from": _header(message, "From"),
            "to": _header(message, "To"),
            "subject": _header(message, "Subject"),
            "date": _header(message, "Date"),
            "body": body_text(message)[:8000],
        }

    def _reply(self, uid: str, body: str) -> EmailMessage:
        conn = self._imap()
        try:
            conn.select("INBOX", readonly=True)
            original = self._fetch(conn, uid)
        finally:
            conn.logout()
        reply = EmailMessage()
        subject = _header(original, "Subject")
        reply["Subject"] = subject if subject.lower().startswith("re:") else f"Re: {subject}"
        reply["From"] = self.address
        reply["To"] = _header(original, "Reply-To") or _header(original, "From")
        reply["Date"] = email.utils.formatdate(localtime=True)
        message_id = original.get("Message-ID")
        if message_id:
            reply["In-Reply-To"] = message_id
            reply["References"] = f"{original.get('References', '')} {message_id}".strip()
        reply.set_content(body)
        return reply

    def save_draft(self, uid: str, body: str) -> dict[str, Any]:
        reply = self._reply(uid, body)
        conn = self._imap()
        try:
            status, _ = conn.append(self._drafts, "(\\Draft)", imaplib.Time2Internaldate(time.time()), reply.as_bytes())
        finally:
            conn.logout()
        if status != "OK":
            raise RuntimeError(f"Saving the draft to '{self._drafts}' failed")
        return {"saved_to": self._drafts, "to": reply["To"], "subject": reply["Subject"]}

    def send_reply(self, uid: str, body: str) -> dict[str, Any]:
        if not self._smtp_host:
            raise RuntimeError("No SMTP server configured (SIRISAI_EMAIL_SMTP_HOST).")
        reply = self._reply(uid, body)
        if self._smtp_port == 465:
            server: smtplib.SMTP = smtplib.SMTP_SSL(self._smtp_host, self._smtp_port, timeout=self._timeout)
        else:
            server = smtplib.SMTP(self._smtp_host, self._smtp_port, timeout=self._timeout)
            if self._ssl:
                server.starttls()
        try:
            server.ehlo()
            # Local relays (a homelab Postfix, a smarthost) often offer no AUTH
            # at all -- found live: logging in there fails even though sending
            # would work.
            if self._password and server.has_extn("auth"):
                server.login(self._username, self._password)
            server.send_message(reply)
        finally:
            server.quit()
        return {"sent": True, "to": reply["To"], "subject": reply["Subject"]}

    def close(self) -> None:
        """Connections are opened per call; nothing to release."""
