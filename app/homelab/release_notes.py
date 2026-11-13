"""What an update changes (6.58).

A container image usually says where it comes from
(`org.opencontainers.image.source`, a GitHub repository) and which version
it is (`org.opencontainers.image.version`). Before offering an update,
SirisAI reads that project's GitHub releases newer than the running
version, links them in the feed item, and flags any whose notes mention a
breaking change, a migration or something incompatible. Releases are kept
a day per repository. SIRISAI_CONTAINER_RELEASE_NOTES=false turns it off.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from app.core.siris import SirisAI

logger = logging.getLogger(__name__)
CACHE_KEY = "containers:release_notes"
CACHE_HOURS = 24
BREAKING = re.compile(r"breaking|migration (?:is )?required|incompatib|manual (?:step|action)|must (?:now )?(?:update|change|migrate)", re.I)
_REPO = re.compile(r"github\.com[/:](?P<owner>[\w.-]+)/(?P<repo>[\w.-]+?)(?:\.git)?/?$")


def repo_of(labels: dict[str, Any]) -> str | None:
    m = _REPO.search(str(labels.get("org.opencontainers.image.source") or labels.get("org.opencontainers.image.url") or ""))
    return f"{m.group('owner')}/{m.group('repo')}" if m else None


def _norm(tag: str) -> str:
    return re.sub(r"^(?:v|version[-_]?|release[-_]?)", "", str(tag).strip().lower())


def fetch(repo: str, transport: httpx.BaseTransport | None = None) -> list[dict[str, Any]]:
    with httpx.Client(timeout=8.0, transport=transport, headers={"Accept": "application/vnd.github+json", "User-Agent": "SirisAI"}) as client:
        response = client.get(f"https://api.github.com/repos/{repo}/releases", params={"per_page": 20})
        response.raise_for_status()
        return [{"tag": r.get("tag_name") or "", "name": r.get("name") or "", "url": r.get("html_url") or "",
                 "published": r.get("published_at") or "", "body": (r.get("body") or "")[:20000], "prerelease": bool(r.get("prerelease"))}
                for r in response.json() if not r.get("draft")]


def releases(siris: "SirisAI", repo: str, now: datetime | None = None, transport: httpx.BaseTransport | None = None) -> list[dict[str, Any]]:
    now = now or datetime.now(UTC)
    cache = dict(siris.state.get(CACHE_KEY) or {})
    hit = cache.get(repo)
    if hit and datetime.fromisoformat(hit["at"]) > now - timedelta(hours=CACHE_HOURS):
        return hit["releases"]
    found = fetch(repo, transport)
    cache[repo] = {"at": now.isoformat(), "releases": [{**r, "body": r["body"][:4000]} for r in found]}
    siris.state.set(CACHE_KEY, dict(list(cache.items())[-60:]))
    return found


def newer_than(found: list[dict[str, Any]], version: str | None, limit: int = 8) -> list[dict[str, Any]]:
    """The releases after `version` (newest first), or the latest few when it isn't known."""
    stable = [r for r in found if not r["prerelease"]] or found
    if version:
        want = _norm(version)
        for i, r in enumerate(stable):
            if _norm(r["tag"]) == want or _norm(r["name"]) == want:
                return stable[:i][:limit]
    return stable[:3]


def summarise(siris: "SirisAI", labels: dict[str, Any], transport: httpx.BaseTransport | None = None) -> dict[str, Any] | None:
    if not siris.settings.container_release_notes:
        return None
    repo = repo_of(labels)
    if not repo:
        return None
    try:
        found = releases(siris, repo, transport=transport)
    except Exception as exc:  # noqa: BLE001 - notes are extra; the update is still offered
        logger.info("Couldn't read %s's releases: %s", repo, exc)
        return {"repo": repo, "error": str(exc)[:200], "releases": [], "breaking": False}
    picked = newer_than(found, labels.get("org.opencontainers.image.version"))
    out = [{"tag": r["tag"], "url": r["url"], "breaking": bool(BREAKING.search(r["body"] + " " + r["name"]))} for r in picked]
    return {"repo": repo, "current": labels.get("org.opencontainers.image.version"), "releases": out,
            "breaking": any(r["breaking"] for r in out)}


def line(container: str, notes: dict[str, Any] | None) -> str:
    if not notes or not notes.get("releases"):
        return ""
    tags = ", ".join(r["tag"] for r in notes["releases"][:4]) + ("..." if len(notes["releases"]) > 4 else "")
    warn = [r for r in notes["releases"] if r["breaking"]]
    text = f"  {container}: {tags} ({notes['releases'][0]['url']})"
    if warn:
        text += f". Check first: {', '.join(r['tag'] for r in warn)} mention{'s' if len(warn) == 1 else ''} a breaking change ({warn[0]['url']})"
    return text
