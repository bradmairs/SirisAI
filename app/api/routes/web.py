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

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response

from app.core.config import Settings, get_settings

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
    "background_color": "#000000",
    "theme_color": "#000000",
    "icons": [
        {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
        {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
    ],
}


def _page(request: Request, settings: Settings, html: Path) -> Response:
    """A web page -- or a redirect to the HTTPS front end (SIRISAI_HTTPS_REDIRECT_URL)
    when it's set and this request didn't already come through it."""
    target = settings.https_redirect_url.rstrip("/")
    if target and request.headers.get("x-forwarded-proto") != "https":
        query = f"?{request.url.query}" if request.url.query else ""
        return RedirectResponse(f"{target}{request.url.path}{query}", status_code=302)
    return HTMLResponse(html.read_text())


@router.get("/", response_class=HTMLResponse)
def index(request: Request, settings: Settings = Depends(get_settings)) -> Response:
    return _page(request, settings, _INDEX_HTML_PATH)


@router.get("/brain", response_class=HTMLResponse)
def brain_map(request: Request, settings: Settings = Depends(get_settings)) -> Response:
    """The second brain's mind map (app/web/static/brain.html), for a browser
    or a Home Assistant webpage card. Open once as /brain?key=YOUR_API_KEY."""
    return _page(request, settings, _STATIC / "brain.html")


@router.get("/hud", response_class=HTMLResponse)
def hud(request: Request, settings: Settings = Depends(get_settings)) -> Response:
    """Full-screen wall-tablet dashboard (app/web/static/hud.html). Open once
    as /hud?key=YOUR_API_KEY to store the key in that browser."""
    return _page(request, settings, _STATIC / "hud.html")


@router.get("/siris-glass.css")
def glass_css() -> FileResponse:
    """SirisOS's Liquid Glass tokens and materials, shared by every page here
    so SirisAI looks like SirisOS (app/web/static/siris-glass.css)."""
    return FileResponse(_STATIC / "siris-glass.css", media_type="text/css", headers={"Cache-Control": "public, max-age=3600"})


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
