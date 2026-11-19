"""What you're watching next (7.27): a watchlist, and the Plex -> Sonarr hand-off.

- **Watchlist**: "remind me to watch Dune" (`watchlist_add`). `plex_suggest`
  puts anything on it that's on Plex first, the daily check says when one
  arrives, and it's crossed off once your history shows you watched it.
- **Next season**: once a day, for each show you watched lately, when you've
  seen the last episode of a season and Sonarr has the next one aired but
  not on disk, the feed asks "Get season 5 of Slow Horses?". Tapping Get it
  monitors that season and searches for it. A show Sonarr doesn't manage, or
  a season that hasn't aired, is left alone.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from app.hub.attention import action, register_action

if TYPE_CHECKING:
    from app.core.siris import SirisAI

logger = logging.getLogger(__name__)

SOURCE = "watching"
_LIST_KEY = "watchlist"


def _key() -> str:
    from app.core.user_context import DEFAULT_USER_ID, get_current_user_id

    who = get_current_user_id() or DEFAULT_USER_ID
    return _LIST_KEY if who == DEFAULT_USER_ID else f"{_LIST_KEY}:{who}"


def watchlist(siris: "SirisAI") -> list[dict[str, Any]]:
    return list(siris.state.get(_key()) or [])


def add(siris: "SirisAI", title: str, note: str = "") -> dict[str, Any]:
    title = title.strip()
    if not title:
        raise ValueError("What should I add to your watchlist?")
    items = [i for i in watchlist(siris) if i["title"].lower() != title.lower()]
    entry = {"title": title, "note": note.strip(), "added": datetime.now(UTC).date().isoformat()}
    siris.state.set(_key(), [*items, entry])
    return entry


def remove(siris: "SirisAI", title: str) -> bool:
    items = watchlist(siris)
    kept = [i for i in items if title.strip().lower() not in i["title"].lower()]
    siris.state.set(_key(), kept)
    return len(kept) < len(items)


def _my_rows(siris: "SirisAI", length: int = 100) -> list[dict[str, Any]]:
    tool = siris.tools.get("tautulli_recent_history")
    if tool is None:
        return []
    found = tool.run(user="me", length=length)
    return found["history"] if isinstance(found, dict) else found


def finished_seasons(rows: list[dict[str, Any]], series: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Shows where the last episode of a season was watched and the next season is aired but missing."""
    by_title = {(s.get("title") or "").lower(): s for s in series}
    watched: dict[str, set[tuple[int, int]]] = {}
    for r in rows:
        if r.get("show") and r.get("season") is not None and r.get("episode") is not None:
            watched.setdefault(r["show"], set()).add((int(r["season"]), int(r["episode"])))
    out = []
    for show, episodes in watched.items():
        found = by_title.get(show.lower())
        if found is None:
            continue
        seasons = {s["season"]: s for s in found["seasons"] if s.get("season")}
        latest = max(season for season, _ in episodes)
        this, nxt = seasons.get(latest), seasons.get(latest + 1)
        if not this or not nxt or not this.get("episodes"):
            continue
        if (latest, int(this["episodes"])) not in episodes:
            continue   # not at the end of the season yet
        if nxt["aired"] and nxt["files"] == 0:
            out.append({"show": found["title"], "series_id": found["id"], "season": latest + 1, "monitored": nxt["monitored"]})
    return out


def check(siris: "SirisAI", now: datetime | None = None) -> dict[str, Any]:
    """The daily check: next seasons to get, watchlist arrivals, and crossing off what you watched."""
    result: dict[str, Any] = {"next_seasons": [], "arrived": [], "watched": []}
    rows = _my_rows(siris)
    sonarr = getattr(siris, "_sonarr_client", None)
    if sonarr is not None and rows:
        try:
            for found in finished_seasons(rows, sonarr.series_seasons()):
                siris.attention.raise_item(
                    f"{SOURCE}:season:{found['series_id']}:{found['season']}",
                    "suggestion",
                    f"Get season {found['season']} of {found['show']}?",
                    source=SOURCE,
                    severity="info",
                    body=f"You've finished season {found['season'] - 1}, and season {found['season']} is out but not on the server.",
                    actions=[action("get_season", "Get it", "primary"), action("dismiss", "No thanks")],
                    payload=found,
                    reopen=False,
                )
                result["next_seasons"].append(found)
        except Exception:
            logger.exception("Next-season check failed")
    seen = {str(r.get("show") or r.get("title") or "").split(" - ")[0].strip().lower() for r in rows}
    plex = getattr(siris, "_plex_client", None)
    for item in watchlist(siris):
        title = item["title"]
        if title.lower() in seen:
            remove(siris, title)
            result["watched"].append(title)
            continue
        if plex is None:
            continue
        try:
            hits = [h for h in plex.search(title) if (h.get("title") or "").lower() == title.lower()]
        except Exception:
            continue
        if hits and not item.get("arrived"):
            item["arrived"] = (now or datetime.now(UTC)).date().isoformat()
            siris.state.set(_key(), [i if i["title"] != title else item for i in watchlist(siris)])
            siris.attention.raise_item(
                f"{SOURCE}:arrived:{title.lower()}", "alert", f"{title} from your watchlist is on Plex",
                source=SOURCE, severity="info", body=item.get("note") or "Ready when you are.",
            )
            result["arrived"].append(title)
    return result


def _get_season(siris: "SirisAI", item: dict[str, Any], payload: dict[str, Any], trigger: str) -> tuple[Any, str]:
    found = payload
    sonarr = getattr(siris, "_sonarr_client", None)
    if sonarr is None:
        raise RuntimeError("Sonarr isn't set up.")
    result = sonarr.get_season(int(found["series_id"]), int(found["season"]))
    return result, f"Searching for season {found['season']} of {found['show']}"


register_action(SOURCE, "get_season", _get_season)
