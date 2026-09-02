"""Serves the built-in web UI (app/web/static/index.html).

A single self-contained HTML/CSS/JS page -- no build step, no external
scripts/styles, so it works over a plain homelab connection (e.g. behind
Tailscale) with no internet access required. It talks to the same-origin
/siris/* API, so no CORS setup is needed.

Not registered under `include_in_schema` since it's not part of the API.
"""

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter(tags=["web"], include_in_schema=False)

_INDEX_HTML_PATH = Path(__file__).resolve().parent.parent.parent / "web" / "static" / "index.html"


@router.get("/", response_class=HTMLResponse)
def index() -> str:
    return _INDEX_HTML_PATH.read_text()
