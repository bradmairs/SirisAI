"""The second brain over HTTP (app/second_brain/): quick capture from
anywhere -- an iOS Shortcut, the share sheet, a Home Assistant automation
-- plus the interactive mind map, what was learned today, insights, and
search.

Capture lands in the vault's inbox (`type: capture`, 00-Inbox/) through the
same engine as every other write, so it's linked straight away and filed
properly later by the vault's /process-inbox workflow. Every route needs
the API key; the mind map page itself is the `/brain` shell in web.py,
which fetches `map.html` from here with the key, because the map embeds
every note.
"""

import re

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from app.core import clock
from app.core.auth import require_api_key
from app.core.siris import SirisAI, get_siris
from app.core.user_context import resolve_user_id
from app.second_brain.client import SecondBrainClient

router = APIRouter(
    prefix="/siris/brain", tags=["second_brain"], dependencies=[Depends(require_api_key), Depends(resolve_user_id)]
)


def _brain(siris: SirisAI) -> SecondBrainClient:
    if siris.second_brain is None:
        raise HTTPException(status_code=404, detail="No second brain configured. Set SIRISAI_BRAIN_PATH.")
    return siris.second_brain


class CaptureRequest(BaseModel):
    text: str = ""
    url: str = ""
    title: str = ""
    tags: list[str] = Field(default_factory=list)


def capture_title(text: str, url: str) -> str:
    """A short, human title: the first line's first eight words, or the
    page a link points to."""
    first = next((line.strip() for line in text.splitlines() if line.strip()), "")
    if first:
        words = re.sub(r"[#*_`>\[\]]", "", first).split()
        return " ".join(words[:8]).rstrip(".,;:!?") + ("…" if len(words) > 8 else "")
    path = re.sub(r"^https?://(www\.)?", "", url).split("?")[0].strip("/")
    return path.replace("/", " – ")[:80] or "Capture"


@router.post("/capture")
def capture(request: CaptureRequest, siris: SirisAI = Depends(get_siris)) -> dict:
    text, url = request.text.strip(), request.url.strip()
    if not text and not url:
        raise HTTPException(status_code=422, detail="Send some text, a url, or both.")
    content = text or url
    if text and url:
        content += f"\n\n{url}"
    item = {
        "title": request.title.strip() or capture_title(text, url),
        "content": content,
        "type": "capture",
        "tags": sorted({"capture", *[t.strip().lower() for t in request.tags if t.strip()]}),
    }
    if url:
        item["source"] = url
    return {"saved": True, **_brain(siris).learn([item])[0]}


@router.get("/map.html", response_class=HTMLResponse)
def mind_map(siris: SirisAI = Depends(get_siris)) -> str:
    return _brain(siris).render_map()


@router.get("/today")
def learned_today(siris: SirisAI = Depends(get_siris)) -> dict:
    date = clock.now_local(siris.settings.timezone).date().isoformat()
    return {"date": date, "items": _brain(siris).learned_today(date)}


@router.get("/search")
def search(q: str, limit: int = 8, siris: SirisAI = Depends(get_siris)) -> list[dict]:
    return _brain(siris).search(q, limit=limit)


@router.get("/insights")
def insights(days: int = Query(30, ge=7, le=365), siris: SirisAI = Depends(get_siris)) -> dict:
    """How the brain is growing and what needs attention (deadlines,
    overdue tasks, idle projects, an old inbox, links worth making)."""
    return _brain(siris).insights(days=days)
