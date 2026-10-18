"""Read-only access to Brad's OneNote notebooks through Microsoft Graph,
for the nightly OneNote import into Siris (app/onenote/importer.py).

OneNote notebooks stored in OneDrive don't sync to disk as readable files,
so the only reliable way to their pages is the OneNote API. It needs a
delegated sign-in: a personal Microsoft account (brad6491@gmail.com) signs in
once with a device code (`python -m app.onenote.login`), granting the app
registration in SIRISAI_ONENOTE_CLIENT_ID read-only `Notes.Read` plus
`offline_access`. The refresh token is kept in SIRISAI_ONENOTE_TOKEN_PATH
(inside ./data, readable by the owner only) and swapped for a fresh access
token on every run. Nothing here can write to OneNote.
"""

from __future__ import annotations

import email
import json
import os
import time
from pathlib import Path
from typing import Any, Callable, Iterator

import httpx

LOGIN = "https://login.microsoftonline.com/consumers/oauth2/v2.0"   # personal Microsoft accounts
GRAPH = "https://graph.microsoft.com/v1.0"
SCOPE = "Notes.Read offline_access"


class OneNoteAuthError(RuntimeError):
    """The sign-in is missing or has expired: run `python -m app.onenote.login` again."""


class TokenStore:
    def __init__(self, path: str) -> None:
        self.path = Path(path)

    def load(self) -> dict | None:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def save(self, tokens: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(tokens), encoding="utf-8")
        os.chmod(self.path, 0o600)


def device_login(client_id: str, store: TokenStore, show: Callable[[str], None] = print,
                 transport: httpx.BaseTransport | None = None, sleep: Callable[[float], None] = time.sleep) -> dict:
    """Sign in with a device code: show() gets the "go to microsoft.com/devicelogin and enter
    ABCD-EFGH" message; this polls until Brad has signed in, then saves the tokens."""
    with httpx.Client(timeout=30, transport=transport) as http:
        start = http.post(f"{LOGIN}/devicecode", data={"client_id": client_id, "scope": SCOPE})
        start.raise_for_status()
        flow = start.json()
        show(flow.get("message") or f"Go to {flow['verification_uri']} and enter {flow['user_code']}")
        interval = int(flow.get("interval", 5))
        deadline = time.monotonic() + int(flow.get("expires_in", 900))
        while time.monotonic() < deadline:
            sleep(interval)
            r = http.post(f"{LOGIN}/token", data={
                "client_id": client_id, "device_code": flow["device_code"],
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code"})
            body = r.json()
            if r.status_code == 200:
                store.save(_stamp(body))
                return body
            error = body.get("error")
            if error == "slow_down":
                interval += 5
            elif error != "authorization_pending":
                raise OneNoteAuthError(body.get("error_description") or error or "sign-in failed")
    raise OneNoteAuthError("the sign-in code expired before it was used")


def _stamp(tokens: dict) -> dict:
    return {**tokens, "expires_at": time.time() + int(tokens.get("expires_in", 3600)) - 60}


class OneNoteClient:
    def __init__(self, client_id: str, store: TokenStore, transport: httpx.BaseTransport | None = None,
                 timeout: float = 60.0) -> None:
        self._client_id = client_id
        self._store = store
        self._http = httpx.Client(timeout=timeout, transport=transport)

    def _token(self) -> str:
        tokens = self._store.load()
        if not tokens or not tokens.get("refresh_token"):
            raise OneNoteAuthError("not signed in to OneNote")
        if tokens.get("access_token") and tokens.get("expires_at", 0) > time.time():
            return tokens["access_token"]
        r = self._http.post(f"{LOGIN}/token", data={
            "client_id": self._client_id, "grant_type": "refresh_token",
            "refresh_token": tokens["refresh_token"], "scope": SCOPE})
        if r.status_code != 200:
            raise OneNoteAuthError(r.json().get("error_description", "the OneNote sign-in has expired"))
        fresh = r.json()
        self._store.save(_stamp({**fresh, "refresh_token": fresh.get("refresh_token") or tokens["refresh_token"]}))
        return fresh["access_token"]

    def _get(self, url: str, **params: Any) -> httpx.Response:
        r = self._http.get(url if url.startswith("http") else GRAPH + url, params=params or None,
                           headers={"Authorization": f"Bearer {self._token()}"})
        r.raise_for_status()
        return r

    def _paged(self, url: str, **params: Any) -> Iterator[dict]:
        page = self._get(url, **params).json()
        while True:
            yield from page.get("value", [])
            nxt = page.get("@odata.nextLink")
            if not nxt:
                return
            page = self._get(nxt).json()

    def sections(self) -> list[dict]:
        """Every section, with its notebook: {id, displayName, parentNotebook: {displayName}}."""
        return list(self._paged("/me/onenote/sections", **{
            "$select": "id,displayName", "$expand": "parentNotebook($select=id,displayName)"}))

    def pages(self, section_id: str) -> list[dict]:
        """A section's pages: {id, title, createdDateTime, lastModifiedDateTime}."""
        return list(self._paged(f"/me/onenote/sections/{section_id}/pages", **{
            "$select": "id,title,createdDateTime,lastModifiedDateTime", "$top": "100"}))

    def page_content(self, page_id: str) -> tuple[str, str]:
        """(html, inkml) for a page. Handwriting comes as InkML in a second part of a
        multipart reply; a page with no ink is plain HTML."""
        r = self._get(f"/me/onenote/pages/{page_id}/content", includeInkML="true")
        ctype = r.headers.get("content-type", "")
        if not ctype.startswith("multipart/"):
            return r.text, ""
        msg = email.message_from_bytes(f"Content-Type: {ctype}\r\n\r\n".encode() + r.content)
        html = ink = ""
        for part in msg.walk():
            kind = part.get_content_type()
            payload = part.get_payload(decode=True)
            if payload is None:
                continue
            text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
            if kind == "text/html":
                html = text
            elif "inkml" in kind:
                ink = text
        return html, ink

    def resource(self, url: str) -> bytes:
        """An image or file a page embeds (its src points at a Graph resource)."""
        return self._get(url).content

    def close(self) -> None:
        self._http.close()
