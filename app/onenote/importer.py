"""The nightly OneNote import (SIRISAI_ONENOTE_HOUR_LOCAL, before the 2:00
upkeep): new and changed pages from Brad's OneNote notebooks into Siris.

- Typed content becomes Markdown (app/onenote/convert.py); embedded images
  are saved in the vault's 03-Resources/Attachments/OneNote and embedded.
- Handwriting (OneNote ink) is drawn into images, kept with the note, and
  transcribed by the local vision model (SIRISAI_VISION_MODEL), marked as a
  machine transcription to check.
- A new page goes to Siris' inbox, so the upkeep's sort files it as a new
  note or merges it into the note on the same topic. A page edited later
  adds only what changed, as a dated update on wherever the page ended up
  (following a merge).
- At most SIRISAI_ONENOTE_MAX_PAGES pages a night, oldest first, so the
  first import of a big notebook spreads over a few nights instead of
  holding the GPU for hours.

Page ids, dates and the text last imported are kept in the state store.

Finding pages: OneNote allows roughly 400 requests an hour per app and
user, and a big account can't list every page in one paged call (Graph
error 20266 past a section limit) -- only section by section. So each
section's page list is kept in the state store and re-listed only when
the section is new or its lastModifiedDateTime has moved, plus a few of
the longest-unchecked sections each night as a safety net; at most
_MAX_LISTINGS listings a night, so the first full listing spreads over a
few nights alongside the imports.
"""

from __future__ import annotations

import difflib
import hashlib
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from app.onenote.convert import html_to_markdown, parse_inkml, render_strokes
from app.onenote.graph import OneNoteAuthError, OneNoteClient, TokenStore

if TYPE_CHECKING:
    from app.core.siris import SirisAI

logger = logging.getLogger(__name__)

STATE_KEY = "onenote_pages"          # {page id: {modified, title, text, ink}}
SECTIONS_KEY = "onenote_sections"    # {section id: {modified, listed, notebook, name, pages: [...]}}
LAST_RUN_KEY = "onenote_last_run"    # the last run's outcome, for the upkeep summary
_MAX_LISTINGS = 100                  # section page-listings a night
_RECHECKS = 10                       # of those, unchanged sections re-listed as a safety net
AUTH_ALERT_KEY = "onenote_auth_alerted"
ATTACHMENTS = "03-Resources/Attachments/OneNote"

TRANSCRIBE_PROMPT = (
    "This is a page of handwritten notes. Transcribe it into Markdown exactly as written: keep the "
    "wording and line breaks. Use '#' headings for titles, '- ' for bullet points, '- [ ] ' for tasks "
    "and to-dos, and a table where the writing is a table. Write [illegible] for any word you can't "
    "read; never guess. Describe a sketch or diagram in one italic line. Reply with only the Markdown."
)


def _safe(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|#^\[\]]+', " ", name).strip()[:80] or "Untitled"


def _ext(data: bytes) -> str:
    return "jpg" if data[:3] == b"\xff\xd8\xff" else "gif" if data[:4] == b"GIF8" else "png"


def _push(siris: "SirisAI", message: str, title: str, priority: str | None = None) -> None:
    from app.second_brain.jobs import _push as push
    push(siris, message, title, priority)


class _Page:
    """One page's content, made ready for Siris."""

    def __init__(self, siris: "SirisAI", client: OneNoteClient, meta: dict, notebook: str, section: str) -> None:
        self.siris, self.client, self.meta = siris, client, meta
        self.notebook, self.section = notebook, section
        self.vault = Path(siris.second_brain.path)
        self.images = 0

    def _save(self, name: str, data: bytes) -> str:
        path = self.vault / ATTACHMENTS / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return f"![[{name}]]"

    def build(self, title: str) -> dict[str, Any]:
        html, ink = self.client.page_content(self.meta["id"])

        def image(src: str, alt: str) -> str:
            try:
                data = self.client.resource(src)
            except httpx.HTTPError:
                return f"*(an image that couldn't be downloaded{': ' + alt if alt else ''})*"
            self.images += 1
            return self._save(f"{_safe(title)} - image {self.images}.{_ext(data)}", data)

        _, typed = html_to_markdown(html, image)
        handwriting, tiles = "", render_strokes(parse_inkml(ink)) if ink else []
        if tiles:
            embeds = [self._save(f"{_safe(title)} - handwriting {i}.png", t) for i, t in enumerate(tiles, 1)]
            vision = getattr(self.siris, "vision", None)
            if vision is not None:
                parts = []
                for tile in tiles:
                    try:
                        parts.append(vision.describe([tile], TRANSCRIBE_PROMPT).strip())
                    except httpx.HTTPError as exc:   # the model's down or too slow: the images are still kept
                        logger.warning("OneNote handwriting transcription failed: %s", exc)
                        parts.append("*(this part couldn't be transcribed)*")
                handwriting = ("*Transcribed from handwriting by the local vision model: check it against the "
                               "images below.*\n\n" + "\n\n".join(p for p in parts if p))
            else:
                handwriting = "*Handwriting not transcribed: set SIRISAI_VISION_MODEL to have it read.*"
            handwriting += "\n\n" + "\n".join(embeds)
        return {"typed": typed, "handwriting": handwriting, "ink": hashlib.sha1(ink.encode()).hexdigest() if ink else ""}


def _content(page: dict, built: dict, notebook: str, section: str) -> str:
    created = (page.get("createdDateTime") or "")[:10]
    parts = [f"*From OneNote: {notebook} › {section}{', created ' + created if created else ''}.*"]
    if built["typed"]:
        parts.append(built["typed"])
    if built["handwriting"]:
        parts.append("## Handwriting\n\n" + built["handwriting"])
    return "\n\n".join(parts)


def _follow(siris: "SirisAI", title: str) -> str:
    """Where a page's note is now: the sort may have merged it into another note."""
    note = siris.second_brain.get_note(title)
    meta = (note or {}).get("meta") or {}
    return meta.get("merged_into") or (note or {}).get("title") or title


def run_import(siris: "SirisAI", now: datetime, client: OneNoteClient | None = None) -> dict[str, Any]:
    s = siris.settings
    if not s.onenote_client_id:
        return {"skipped": "OneNote isn't set up"}
    store = TokenStore(s.onenote_token_path)
    if client is None:
        if store.load() is None:
            return {"skipped": "not signed in: run python -m app.onenote.login"}
        client = OneNoteClient(s.onenote_client_id, store)
    try:
        result = _run(siris, client, now)
        siris.state.set(AUTH_ALERT_KEY, False)
    except OneNoteAuthError as exc:
        if not siris.state.get(AUTH_ALERT_KEY):
            _push(siris, "Siris can't read OneNote any more: the Microsoft sign-in has expired. On the server, run "
                         "`docker exec -it sirisai python -m app.onenote.login` (container `sirisai-sirisai-1` "
                         "if SirisAI runs on its own).",
                  "Siris: OneNote sign-in needed", "high")
            siris.state.set(AUTH_ALERT_KEY, True)
        result = {"error": f"OneNote sign-in: {exc}"[:200]}
    finally:
        client.close()
    from app.core import clock
    siris.state.set(LAST_RUN_KEY, {"date": now.astimezone(clock.get_zone(s.timezone)).date().isoformat(), **result})
    return result


def _throttled(exc: httpx.HTTPStatusError) -> bool:
    return exc.response.status_code == 429


def _refresh_sections(siris: "SirisAI", client: OneNoteClient, now: datetime) -> tuple[dict[str, dict], int, bool]:
    """Bring the kept section page lists up to date within tonight's budget.
    Returns (sections, how many still need listing, throttled)."""
    cache: dict[str, dict] = dict(siris.state.get(SECTIONS_KEY) or {})
    try:
        live = client.sections()
    except httpx.HTTPStatusError as exc:
        if _throttled(exc):
            return cache, 0, True
        raise
    live_ids = {sec["id"] for sec in live}
    cache = {sid: c for sid, c in cache.items() if sid in live_ids}   # deleted sections go
    changed, unchanged = [], []
    for sec in live:
        kept = cache.get(sec["id"])
        if kept is None or sec.get("lastModifiedDateTime", "") > kept.get("modified", ""):
            changed.append(sec)
        else:
            unchanged.append(sec)
    changed.sort(key=lambda sec: (sec["id"] in cache, cache.get(sec["id"], {}).get("listed", "")))
    unchanged.sort(key=lambda sec: cache[sec["id"]].get("listed", ""))
    todo = changed[:_MAX_LISTINGS] + unchanged[:max(0, min(_RECHECKS, _MAX_LISTINGS - len(changed)))]
    throttled = False
    for done, sec in enumerate(todo):
        try:
            pages = client.pages(sec["id"])
        except httpx.HTTPStatusError as exc:
            if not _throttled(exc):
                raise
            throttled = True
            todo = todo[:done]
            break
        cache[sec["id"]] = {
            "modified": sec.get("lastModifiedDateTime", ""), "listed": now.isoformat(),
            "notebook": (sec.get("parentNotebook") or {}).get("displayName", ""),
            "name": sec.get("displayName", ""), "pages": pages,
        }
        siris.state.set(SECTIONS_KEY, cache)   # after each listing, so a throttle keeps what's done
    listed = {sec["id"] for sec in todo}
    remaining = sum(1 for sec in changed if sec["id"] not in listed)
    return cache, remaining, throttled


def _run(siris: "SirisAI", client: OneNoteClient, now: datetime) -> dict[str, Any]:
    s, brain = siris.settings, siris.second_brain
    wanted = {n.strip().lower() for n in s.onenote_notebooks.split(",") if n.strip()}
    seen: dict[str, dict] = dict(siris.state.get(STATE_KEY) or {})
    sections, unlisted, throttled = _refresh_sections(siris, client, now)
    due = []
    for section in sections.values():
        notebook = section.get("notebook", "")
        if wanted and notebook.lower() not in wanted:
            continue
        for page in section.get("pages", []):
            known = seen.get(page["id"])
            if known is None or page.get("lastModifiedDateTime", "") > known.get("modified", ""):
                due.append((page, notebook, section.get("name", "")))
    due.sort(key=lambda d: d[0].get("lastModifiedDateTime", ""))
    out: dict[str, Any] = {"imported": [], "updated": [], "handwritten": 0, "waiting": max(0, len(due) - s.onenote_max_pages)}
    if unlisted:
        out["sections_to_list"] = unlisted
    if throttled:
        out["throttled"] = "OneNote's hourly request limit; the rest carries on tomorrow night"
        return out
    used = {v.get("title", "").lower() for v in seen.values()}
    for page, notebook, section in due[:s.onenote_max_pages]:
        try:
            known = seen.get(page["id"])
            title = known["title"] if known else (page.get("title") or "").strip() or \
                f"{section} {(page.get('createdDateTime') or '')[:10]}".strip()
            if not known and title.lower() in used:   # two pages called "Notes": keep them apart
                title = f"{title} ({section})"
            built = _Page(siris, client, page, notebook, section).build(title)
            out["handwritten"] += bool(built["ink"])
            if known is None:
                result = brain.learn([{
                    "title": title, "content": _content(page, built, notebook, section),
                    "source": f"OneNote: {notebook} › {section} › {page.get('title') or title}",
                    "tags": ["onenote"] + (["handwriting"] if built["ink"] else []), "inbox_if_new": True,
                }])[0]
                title = result["title"]
                out["imported"].append(title)
            else:
                old = known.get("text", "").splitlines()
                added = [l[2:] for l in difflib.ndiff(old, built["typed"].splitlines()) if l.startswith("+ ") and l[2:].strip()]
                if built["ink"] and built["ink"] != known.get("ink"):
                    added.append("## Handwriting (updated)\n\n" + built["handwriting"])
                if added:
                    target = _follow(siris, title)
                    brain.learn([{"title": target, "content": "Updated in OneNote:\n\n" + "\n".join(added),
                                  "source": f"OneNote: {notebook} › {section} › {page.get('title') or title}"}])
                    out["updated"].append(target)
            seen[page["id"]] = {"modified": page.get("lastModifiedDateTime", ""), "title": title,
                                "text": built["typed"], "ink": built["ink"]}
            used.add(title.lower())
            siris.state.set(STATE_KEY, seen)   # after each page, so a failure keeps what's done
        except OneNoteAuthError:
            raise
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 429:   # throttled: carry on tomorrow night
                out["waiting"] += 1
                break
            logger.warning("OneNote page %s failed: %s", page.get("id"), exc)
            out.setdefault("failed", []).append(page.get("title") or page["id"])
        except Exception as exc:  # noqa: BLE001 - one bad page never stops the rest; it's retried next night
            logger.warning("OneNote page %s failed: %s", page.get("id"), exc)
            out.setdefault("failed", []).append(page.get("title") or page["id"])
    return out
