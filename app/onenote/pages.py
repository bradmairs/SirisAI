"""A handwritten OneNote page drawn again, for SirisOS's handwriting review: Brad sees the page beside
its transcription. The vault keeps the transcription, not the images, so the page's ink is fetched from
OneNote (read-only) and drawn the way the import drew it, then kept in data/onenote/pages/ so each page
is fetched once.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Callable

from app.onenote.convert import parse_inkml, render_strokes
from app.onenote.graph import OneNoteClient, TokenStore
from app.onenote.importer import STATE_KEY

if TYPE_CHECKING:
    from app.core.siris import SirisAI


class PageNotFound(LookupError):
    """No OneNote page is known for that note, or OneNote isn't set up."""


def page_id(siris: "SirisAI", names: list[str]) -> str:
    """The OneNote page a note came from, by the title the import gave it (or one it was renamed from)."""
    wanted = {n.lower() for n in names if n}
    for pid, entry in (siris.state.get(STATE_KEY) or {}).items():
        if str(entry.get("title") or "").lower() in wanted:
            return pid
    raise PageNotFound("No OneNote page is known for that note.")


def page_images(siris: "SirisAI", names: list[str],
                client_factory: Callable[[], OneNoteClient] | None = None) -> list[bytes]:
    """The page's handwriting as PNG tiles, top to bottom."""
    pid = page_id(siris, names)
    cache = Path(siris.settings.onenote_token_path).parent / "pages" / pid.replace("/", "_")
    tiles = sorted(cache.glob("*.png"))
    if tiles:
        return [t.read_bytes() for t in tiles]
    s = siris.settings
    if client_factory is None:
        if not s.onenote_client_id or TokenStore(s.onenote_token_path).load() is None:
            raise PageNotFound("OneNote isn't signed in.")
        client_factory = lambda: OneNoteClient(s.onenote_client_id, TokenStore(s.onenote_token_path))  # noqa: E731
    client = client_factory()
    try:
        _, ink = client.page_content(pid)
    finally:
        client.close()
    images = render_strokes(parse_inkml(ink)) if ink else []
    cache.mkdir(parents=True, exist_ok=True)
    for i, png in enumerate(images, 1):
        (cache / f"{i:02d}.png").write_bytes(png)
    return images
