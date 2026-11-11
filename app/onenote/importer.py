"""The nightly OneNote import (SIRISAI_ONENOTE_HOUR_LOCAL, before the 2:00
upkeep): new and changed pages from Brad's OneNote notebooks into Siris.

- Typed content becomes Markdown (app/onenote/convert.py); embedded images
  are saved in the vault's 03-Resources/Attachments/OneNote and embedded.
- Handwriting (OneNote ink) is drawn into images, kept with the note, and
  transcribed by the local vision model (SIRISAI_VISION_MODEL), marked as a
  machine transcription to check.
- A new page goes to Siris' inbox, so the upkeep's sort files it as a new
  note, merges it into the note on the same topic, or splits it across the
  notes it covers. A page edited later adds only what changed, as a dated
  update on wherever the page ended up (following a merge); for a page that
  was split, what changed goes to the inbox to be sorted the same way.
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
import fnmatch
import hashlib
import io
import logging
import re
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
from PIL import Image

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
_MAX_CHECKS = 120                    # pages read a night, imported or skipped (each read is a request)
AUTH_ALERT_KEY = "onenote_auth_alerted"
ATTACHMENTS = "03-Resources/Attachments/OneNote"

TRANSCRIBE_PROMPT = (
    "This is a page of handwritten notes. Transcribe it into Markdown exactly as written: keep the "
    "wording and line breaks. Use '#' headings for titles, '- ' for bullet points, '- [ ] ' for tasks "
    "and to-dos, and a table where the writing is a table. Write [illegible] for any word you can't "
    "read; never guess. Describe a sketch or diagram in one italic line. Reply with only the Markdown."
)


PROOFREAD_PROMPT = (
    "Below is a first-pass transcription of the handwritten page in the image. Check it against the image "
    "word by word. Fix words that were misread, using the image and the context (an engineer's notes: water "
    "and wastewater projects, design, meetings, training), and fix spelling mistakes. Keep the layout and "
    "Markdown. Keep numbers, units, names and maths exactly as written. Keep [illegible] only where the "
    "writing truly can't be read; never add anything that isn't written. Reply with only the corrected "
    "Markdown.\n\nFirst pass:\n\n"
)
_MIN_PROOFREAD_SIMILARITY = 0.5   # a "correction" less alike than this rewrote the page: keep the first pass


def _proofread(vision: Any, tile: bytes, draft: str) -> str:
    """A second look by the vision model, with its own draft beside the image:
    misreads that don't make sense and spelling get fixed. A reply that isn't
    a correction of the draft (too different, or empty) is ignored."""
    if not draft.strip():
        return draft
    try:
        fixed = vision.describe([tile], PROOFREAD_PROMPT + draft).strip()
    except httpx.HTTPError as exc:
        logger.warning("OneNote proofread failed: %s", exc)
        return draft
    if not fixed or difflib.SequenceMatcher(None, draft, fixed).ratio() < _MIN_PROOFREAD_SIMILARITY:
        return draft
    return fixed


def _safe(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|#^\[\]]+', " ", name).strip()[:80] or "Untitled"


def _ext(data: bytes) -> str:
    return "jpg" if data[:3] == b"\xff\xd8\xff" else "gif" if data[:4] == b"GIF8" else "png"


_SHRINK_OVER_BYTES = 300_000
_MAX_IMAGE_SIDE = 1600


def _shrink(data: bytes) -> tuple[bytes, str]:
    """Pasted printouts and slides arrive as PNGs of up to 15 MB (89 MB for Brad's
    first 30 pages, so gigabytes in the vault's git history over a whole archive).
    A big image is scaled to at most _MAX_IMAGE_SIDE and saved as JPEG on white --
    still readable, a fraction of the size. Small ones are kept exactly."""
    if len(data) <= _SHRINK_OVER_BYTES:
        return data, _ext(data)
    try:
        with Image.open(io.BytesIO(data)) as im:
            if getattr(im, "is_animated", False):
                return data, _ext(data)
            im.thumbnail((_MAX_IMAGE_SIDE, _MAX_IMAGE_SIDE))
            if im.mode in ("RGBA", "LA", "P"):
                rgba = im.convert("RGBA")
                flat = Image.new("RGB", rgba.size, "white")
                flat.paste(rgba, mask=rgba.getchannel("A"))
            else:
                flat = im.convert("RGB")
            out = io.BytesIO()
            flat.save(out, "JPEG", quality=82, optimize=True)
    except Exception:  # noqa: BLE001 - not an image Pillow can read: keep it as it came
        return data, _ext(data)
    small = out.getvalue()
    return (small, "jpg") if len(small) < len(data) else (data, _ext(data))


_SLASH_DATE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{2}(?:\d{2})?)\b")
# The same rules as the vault engine's onenote_title (tools/brain.py), which refiled the older pages.
_GENERIC_SECTION = re.compile(r"^(new section \d*|other sections?|general|misc\w*|notes|meetings?|in person meetings?|untitled.*)$", re.I)
_GENERIC_TITLE = re.compile(r"^(session|speaker|meeting|meetings|initial notes|notes|workshop|planning|introduction|intro|"
                            r"catch-?up|weekly meeting|update|untitled|new section|questions?|minutes|agenda|summary|"
                            r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*)\b", re.I)


def _iso_dates(text: str) -> str:
    """"17/3/22" (day first) as 2022-03-17, so a date survives being a file name."""
    def fix(m: re.Match) -> str:
        d, mo, y = int(m.group(1)), int(m.group(2)), m.group(3)
        try:
            return date(int(y) + (2000 if len(y) == 2 else 0), mo, d).isoformat()
        except ValueError:
            return m.group(0)
    return _SLASH_DATE.sub(fix, text)


def _page_title(title: str, notebook: str, section: str) -> str:
    """A note name that makes sense out of its notebook. OneNote titles like "1a", "J" or "-" get
    the notebook and section in front; generic ones ("Session 1", "Initial Notes", "Speaker 2:
    Tina Porou") get their section (or the notebook, for "New Section 1"); dates become YYYY-MM-DD."""
    title = _iso_dates(title)
    context = notebook if _GENERIC_SECTION.match(section) else section
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", title.strip()):   # a page titled only "19/07/23": the meeting series it's in
        return f"{context} – {title.strip()}"
    if sum(c.isalpha() for c in title) < 3:
        rest = f" - {title}" if any(c.isalnum() for c in title) else ""
        return f"{notebook} - {section}{rest}".strip(" -")
    speaker = re.match(r"(?i)speaker \d+\s*:\s*", title)
    if speaker:
        title = title[speaker.end():]
    bare = re.sub(r"\d{4}-\d{2}-\d{2}|[-–:#\d\s]+", " ", title).split()
    stem = lambda t: {w.rstrip("s") for w in re.findall(r"[a-z0-9]{3,}", t.lower())}  # noqa: E731
    if (len(bare) <= 2 or _GENERIC_TITLE.match(title) or speaker) and not stem(context) & stem(title):
        title = f"{context} – {title}"
    return re.sub(r"\s+", " ", title).strip(" -–")


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
        if self.siris.settings.onenote_skip_image_pages and _has_images(html):
            return {"skipped": "has images or PDF printouts"}

        save = self.siris.settings.onenote_save_images

        def image(src: str, alt: str) -> str:
            if not save:
                return f"*(a picture{': ' + alt if alt else ''}: see the page in OneNote)*"
            try:
                data = self.client.resource(src)
            except httpx.HTTPError:
                return f"*(an image that couldn't be downloaded{': ' + alt if alt else ''})*"
            self.images += 1
            data, ext = _shrink(data)
            return self._save(f"{_safe(title)} - image {self.images}.{ext}", data)

        _, typed = html_to_markdown(html, image)
        handwriting, tiles = "", render_strokes(parse_inkml(ink)) if ink else []
        if tiles:
            embeds = [self._save(f"{_safe(title)} - handwriting {i}.png", t) for i, t in enumerate(tiles, 1)] if save else []
            against = "the images below" if save else "the page in OneNote"
            vision = getattr(self.siris, "vision", None)
            if vision is not None:
                parts = []
                for tile in tiles:
                    try:
                        draft = vision.describe([tile], TRANSCRIBE_PROMPT).strip()
                        parts.append(_proofread(vision, tile, draft))
                    except httpx.HTTPError as exc:   # the model's down or too slow: the images are still kept
                        logger.warning("OneNote handwriting transcription failed: %s", exc)
                        parts.append("*(this part couldn't be transcribed)*")
                handwriting = (f"*Transcribed from handwriting and proofread by the local vision model: check it against "
                               f"{against}.*\n\n" + "\n\n".join(p for p in parts if p))
            else:
                handwriting = "*Handwriting not transcribed: set SIRISAI_VISION_MODEL to have it read.*"
            if embeds:
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


def _follow(siris: "SirisAI", title: str) -> str | None:
    """Where a page's note is now: the sort may have merged it into another note. None when
    the sort split it across several notes: there's no one note for an edit to go on."""
    note = siris.second_brain.get_note(title)
    meta = (note or {}).get("meta") or {}
    if meta.get("split_into"):
        return None
    return meta.get("merged_into") or (note or {}).get("title") or title


def run_import(siris: "SirisAI", now: datetime, client: OneNoteClient | None = None) -> dict[str, Any]:
    s = siris.settings
    if not s.onenote_import or not s.onenote_client_id:  # 5.63: off unless SIRISAI_ONENOTE_IMPORT is on
        return {"skipped": "OneNote import is off (SIRISAI_ONENOTE_IMPORT)" if s.onenote_client_id else "OneNote isn't set up"}
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


def _included(notebook: str, settings: Any) -> bool:
    """SIRISAI_ONENOTE_NOTEBOOKS (only these, if set) and SIRISAI_ONENOTE_SKIP_NOTEBOOKS
    (never these; `*` wildcards), both case-insensitive."""
    name = notebook.lower()
    wanted = {n.strip().lower() for n in settings.onenote_notebooks.split(",") if n.strip()}
    skipped = [n.strip().lower() for n in settings.onenote_skip_notebooks.split(",") if n.strip()]
    return (not wanted or name in wanted) and not any(fnmatch.fnmatchcase(name, pattern) for pattern in skipped)


def _has_images(html: str) -> bool:
    """Pictures, including PDF printouts (which OneNote serves as images)."""
    return re.search(r"<img\b", html, flags=re.I) is not None


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
    live = [sec for sec in live if _included((sec.get("parentNotebook") or {}).get("displayName", ""), siris.settings)]
    live_ids = {sec["id"] for sec in live}
    cache = {sid: c for sid, c in cache.items() if sid in live_ids}   # deleted or excluded sections go
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
    seen: dict[str, dict] = dict(siris.state.get(STATE_KEY) or {})
    sections, unlisted, throttled = _refresh_sections(siris, client, now)
    due = []
    for section in sections.values():
        notebook = section.get("notebook", "")
        if not _included(notebook, s):
            continue
        for page in section.get("pages", []):
            known = seen.get(page["id"])
            if known is None or page.get("lastModifiedDateTime", "") > known.get("modified", ""):
                due.append((page, notebook, section.get("name", "")))
    due.sort(key=lambda d: d[0].get("lastModifiedDateTime", ""))
    out: dict[str, Any] = {"imported": [], "updated": [], "handwritten": 0, "skipped": 0, "waiting": len(due)}
    if unlisted:
        out["sections_to_list"] = unlisted
    if throttled:
        out["throttled"] = "OneNote's hourly request limit; the rest carries on tomorrow night"
        return out
    used = {v["title"].lower() for v in seen.values() if v.get("title")}
    handled = checked = 0
    for page, notebook, section in due:
        if handled >= s.onenote_max_pages or checked >= _MAX_CHECKS:
            break
        checked += 1
        try:
            known = seen.get(page["id"])
            if known is not None and known.get("skipped"):
                known = None   # skipped before and edited since: look at it afresh
            title = known["title"] if known else (page.get("title") or "").strip() or \
                f"{section} {(page.get('createdDateTime') or '')[:10]}".strip()
            if not known:
                title = _page_title(title, notebook, section)
            if not known and title.lower() in used:   # two pages called "Notes": keep them apart
                title = f"{title} ({section})"
            built = _Page(siris, client, page, notebook, section).build(title)
            if not built.get("skipped") and not built["ink"] and not built["typed"].strip(" \n`"):
                built = {"skipped": "empty page"}   # a title and nothing else adds nothing to Siris
            if built.get("skipped"):
                seen[page["id"]] = {"modified": page.get("lastModifiedDateTime", ""), "skipped": built["skipped"]}
                siris.state.set(STATE_KEY, seen)
                out["skipped"] += 1
                out["waiting"] -= 1
                continue
            handled += 1
            out["handwritten"] += bool(built["ink"])
            if known is None:
                result = brain.learn([{
                    "title": title, "content": _content(page, built, notebook, section),
                    "source": f"OneNote: {notebook} › {section} › {page.get('title') or title}",
                    "tags": ["onenote"] + (["handwriting"] if built["ink"] else []), "inbox_if_new": True,
                }])[0]
                if result.get("action") == "skipped":   # Brad deleted this page's note: it stays deleted
                    seen[page["id"]] = {"modified": page.get("lastModifiedDateTime", ""), "skipped": "deleted in Siris"}
                    siris.state.set(STATE_KEY, seen)
                    out["skipped"] += 1
                    out["waiting"] -= 1
                    continue
                title = result["title"]
                out["imported"].append(title)
            else:
                old = known.get("text", "").splitlines()
                added = [l[2:] for l in difflib.ndiff(old, built["typed"].splitlines()) if l.startswith("+ ") and l[2:].strip()]
                if built["ink"] and built["ink"] != known.get("ink"):
                    added.append("## Handwriting (updated)\n\n" + built["handwriting"])
                if added:
                    target = _follow(siris, title)
                    source = f"OneNote: {notebook} › {section} › {page.get('title') or title}"
                    if target is None:
                        # The page was split across the notes it covers: what's new waits in the inbox,
                        # where the nightly sort puts each part of it on its note the same way.
                        from app.core import clock

                        day = now.astimezone(clock.get_zone(s.timezone)).date().isoformat()
                        result = brain.learn([{"title": f"{title} (updated {day})", "content": "\n".join(added),
                                               "source": source, "tags": ["onenote"], "inbox_if_new": True}])[0]
                        out["updated"].append(result["title"])
                    else:
                        result = brain.learn([{"title": target, "content": "Updated in OneNote:\n\n" + "\n".join(added),
                                               "source": source}])[0]
                        if result.get("action") != "skipped":   # skipped: its note was deleted, and stays so
                            out["updated"].append(target)
            seen[page["id"]] = {"modified": page.get("lastModifiedDateTime", ""), "title": title,
                                "text": built["typed"], "ink": built["ink"]}
            used.add(title.lower())
            siris.state.set(STATE_KEY, seen)   # after each page, so a failure keeps what's done
            out["waiting"] -= 1
        except OneNoteAuthError:
            raise
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 429:   # throttled: carry on tomorrow night
                break
            logger.warning("OneNote page %s failed: %s", page.get("id"), exc)
            out.setdefault("failed", []).append(page.get("title") or page["id"])
        except Exception as exc:  # noqa: BLE001 - one bad page never stops the rest; it's retried next night
            logger.warning("OneNote page %s failed: %s", page.get("id"), exc)
            out.setdefault("failed", []).append(page.get("title") or page["id"])
    return out
