"""News and podcasts for the morning brief (SirisAI 5.0).

- **News**: the RSS or Atom feeds in SIRISAI_NEWS_FEEDS, from the last day.
  With SIRISAI_NEWS_TOPICS set ("AI, Melbourne, Formula 1"), only stories
  that mention one; otherwise the top of each feed. The model turns the
  headlines into a short spoken roundup (news isn't private, so whichever
  model is set up). Without a model, the headlines are read as they are.
- **Podcasts**: new episodes since yesterday from SIRISAI_PODCAST_FEEDS,
  by show.
- **Overnight**: what came into the inbox since last night, the jobs that
  failed and the doors or sensors that need you (the attention feed).

The spoken morning brief and the written one both add these. GET
/siris/news gives them as data; news_brief answers "what's in the news".
Feeds are cached for SIRISAI_NEWS_CACHE_MINUTES (30).
"""

from __future__ import annotations

import logging
import re
import time
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from html import unescape
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from app.core.siris import SirisAI

logger = logging.getLogger(__name__)
transport: httpx.BaseTransport | None = None
_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_TAG = re.compile(r"<[^>]+>")
MAX_BYTES = 3 * 1024 * 1024


def _text(node: ET.Element | None) -> str:
    return " ".join(unescape(_TAG.sub(" ", node.text or "")).split()) if node is not None else ""


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child(node: ET.Element, *names: str) -> ET.Element | None:
    for child in node:
        if _local(child.tag) in names:
            return child
    return None


def _when(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return parsedate_to_datetime(value).astimezone(UTC)
    except (TypeError, ValueError):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
        except ValueError:
            return None


def parse(xml: bytes, source: str = "") -> list[dict[str, Any]]:
    """Items from an RSS 2.0 or Atom feed: title, link, summary, published, source."""
    root = ET.fromstring(xml)
    channel = _child(root, "channel")
    title = _text(_child(channel, "title")) if channel is not None else _text(_child(root, "title"))
    entries = [n for n in (channel if channel is not None else root) if _local(n.tag) in ("item", "entry")]
    out = []
    for e in entries:
        link_node = _child(e, "link")
        link = (link_node.get("href") if link_node is not None and link_node.get("href") else _text(link_node)) if link_node is not None else ""
        out.append({
            "title": _text(_child(e, "title")),
            "link": link,
            "summary": _text(_child(e, "description", "summary", "content"))[:400],
            "published": (_when(_text(_child(e, "pubDate", "published", "updated"))) or datetime.now(UTC)).isoformat(),
            "source": title or source,
        })
    return [i for i in out if i["title"]]


def fetch(siris: "SirisAI", url: str) -> list[dict[str, Any]]:
    cached = _CACHE.get(url)
    if cached and time.monotonic() - cached[0] < siris.settings.news_cache_minutes * 60:
        return cached[1]
    with httpx.Client(timeout=15, follow_redirects=True, transport=transport, headers={"User-Agent": "SirisAI news"}) as http:
        response = http.get(url)
        response.raise_for_status()
        items = parse(response.content[:MAX_BYTES], url)
    _CACHE[url] = (time.monotonic(), items)
    return items


def _feeds(value: str) -> list[str]:
    return [u.strip() for u in (value or "").split(",") if u.strip().startswith(("http://", "https://"))]


def stories(siris: "SirisAI", now: datetime | None = None, hours: int = 24) -> list[dict[str, Any]]:
    now = now or datetime.now(UTC)
    topics = [t.strip().lower() for t in (siris.settings.news_topics or "").split(",") if t.strip()]
    picked: list[dict[str, Any]] = []
    for url in _feeds(siris.settings.news_feeds):
        try:
            items = fetch(siris, url)
        except Exception:  # noqa: BLE001 - one dead feed never hides the rest
            logger.info("News feed %s failed", url, exc_info=True)
            continue
        fresh = [i for i in items if datetime.fromisoformat(i["published"]) >= now - timedelta(hours=hours)]
        if topics:
            fresh = [{**i, "topic": next(t for t in topics if t in f"{i['title']} {i['summary']}".lower())}
                     for i in fresh if any(t in f"{i['title']} {i['summary']}".lower() for t in topics)]
        picked.extend(fresh[:4])
    seen, unique = set(), []
    for item in sorted(picked, key=lambda i: i["published"], reverse=True):
        key = re.sub(r"\W+", " ", item["title"].lower())[:60]
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique[: siris.settings.news_items]


def episodes(siris: "SirisAI", now: datetime | None = None, hours: int = 30) -> list[dict[str, Any]]:
    now = now or datetime.now(UTC)
    out = []
    for url in _feeds(siris.settings.podcast_feeds):
        try:
            items = fetch(siris, url)
        except Exception:  # noqa: BLE001
            continue
        out.extend(i for i in items[:3] if datetime.fromisoformat(i["published"]) >= now - timedelta(hours=hours))
    return out


async def roundup(siris: "SirisAI", items: list[dict[str, Any]]) -> str:
    """A short spoken roundup of `items`, or the headlines as they are."""
    if not items:
        return ""
    headlines = "\n".join(f"- {i['title']} ({i['source']}): {i['summary'][:200]}" for i in items)
    if siris.llm is not None and siris.settings.llm_provider != "none":
        try:
            response = await siris.llm.generate([
                {"role": "system", "content": "Turn these headlines into a spoken news roundup of three to five short sentences. "
                 "Plain words, no lists or markdown, no opinions, nothing that isn't in them."},
                {"role": "user", "content": headlines},
            ])
            if response.text:
                return response.text.strip()
        except Exception:  # noqa: BLE001 - the headlines will do
            logger.info("News roundup failed", exc_info=True)
    return "In the news: " + "; ".join(i["title"] for i in items[:5]) + "."


def overnight(siris: "SirisAI", now: datetime | None = None) -> list[str]:
    """Inbox items that came in since 10pm last night (local)."""
    from app.core import clock

    zone = clock.get_zone(siris.settings.timezone)
    local = (now or datetime.now(UTC)).astimezone(zone)
    since = (local - timedelta(days=1)).replace(hour=22, minute=0, second=0, microsecond=0) if local.hour < 22 else local.replace(hour=22, minute=0)
    out = []
    for item in siris.attention.list(limit=100):
        created = item.get("created_at")
        if created and datetime.fromisoformat(created.replace("Z", "+00:00")).astimezone(zone) >= since:
            out.append(item["title"])
    return out


def spoken(siris: "SirisAI", now: datetime | None = None) -> str:
    """The news, podcasts and overnight lines for the spoken morning brief."""
    import asyncio

    parts = []
    came = overnight(siris, now)
    if came:
        parts.append(f"Overnight: {'; '.join(came[:3])}" + (f", and {len(came) - 3} more" if len(came) > 3 else "") + ".")
    items = stories(siris, now)
    if items:
        try:
            parts.append(asyncio.run(roundup(siris, items)))
        except RuntimeError:  # already inside an event loop: the headlines
            parts.append("In the news: " + "; ".join(i["title"] for i in items[:5]) + ".")
    new = episodes(siris, now)
    if new:
        parts.append("New episodes: " + "; ".join(f"{e['source']}, {e['title']}" for e in new[:3]) + ".")
    return " ".join(p for p in parts if p)
