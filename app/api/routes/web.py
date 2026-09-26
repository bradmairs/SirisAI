"""Serves the built-in web UI (app/web/static/index.html) and what makes it
installable on a phone: a web app manifest and home-screen icons.

A single self-contained HTML/CSS/JS page -- no build step, no external
scripts/styles, so it works over a plain homelab connection (e.g. behind
Tailscale) with no internet access required. It talks to the same-origin
/siris/* API, so no CORS setup is needed.

Not registered under `include_in_schema` since it's not part of the API.
"""

from pathlib import Path
from typing import Any

from fastapi import APIRouter
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

router = APIRouter(tags=["web"], include_in_schema=False)

_STATIC = Path(__file__).resolve().parent.parent.parent / "web" / "static"
_INDEX_HTML_PATH = _STATIC / "index.html"

_MANIFEST: dict[str, Any] = {
    "name": "SirisAI",
    "short_name": "SirisAI",
    "description": "Your self-hosted homelab assistant",
    "start_url": "/",
    "scope": "/",
    "display": "standalone",
    "background_color": "#14161a",
    "theme_color": "#14161a",
    "icons": [
        {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
        {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
    ],
}


@router.get("/", response_class=HTMLResponse)
def index() -> str:
    return _INDEX_HTML_PATH.read_text()


@router.get("/manifest.webmanifest")
def manifest() -> JSONResponse:
    return JSONResponse(_MANIFEST, media_type="application/manifest+json")


@router.get("/apple-touch-icon.png")
@router.get("/icon-180.png")
def apple_touch_icon() -> FileResponse:
    return FileResponse(_STATIC / "icon-180.png", media_type="image/png")


@router.get("/icon-192.png")
def icon_192() -> FileResponse:
    return FileResponse(_STATIC / "icon-192.png", media_type="image/png")


@router.get("/icon-512.png")
def icon_512() -> FileResponse:
    return FileResponse(_STATIC / "icon-512.png", media_type="image/png")
