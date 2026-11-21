"""Plex tools -- what's playing and what libraries exist, via
app/plex/client.py. Registered only when SIRISAI_PLEX_BASE_URL and
SIRISAI_PLEX_TOKEN are both set. Find your token at
https://support.plex.tv/articles/204059436.
"""

from collections.abc import Callable
from typing import Any

from app.core import identity
from app.plex.client import PlexClient
from app.plugins.base import Tool, ToolPermission


class PlexNowPlayingTool(Tool):
    name = "plex_now_playing"
    description = (
        "List what's currently playing on Plex -- title, user, playback state, progress. "
        "user='me' gives only the user's own ('what am I watching?')."
    )
    permission = ToolPermission.READ
    parameters = {
        "type": "object",
        "properties": {"user": {"type": "string", "description": "'me' for the user's own. Omit for everyone."}},
        "required": [],
    }

    def __init__(self, client: PlexClient, my_names: Callable[[], list[str]] | None = None) -> None:
        self._client = client
        self._my_names = my_names or (lambda: [])

    def run(self, user: str | None = None, **kwargs: Any) -> Any:
        playing = self._client.now_playing()
        if not identity.is_me(user):
            return playing
        names = self._my_names()  # 7.17: your Plex names (Your accounts), else the server owner's sessions
        if names:
            return [p for p in playing if identity.matches(p.get("user"), names)]
        return [p for p in playing if p.get("owner")]


class PlexLibrarySectionsTool(Tool):
    name = "plex_library_sections"
    description = "List Plex library sections (Movies, TV Shows, Music, etc)."
    permission = ToolPermission.READ
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, client: PlexClient) -> None:
        self._client = client

    def run(self, **kwargs: Any) -> Any:
        return self._client.library_sections()


class PlexSearchTool(Tool):
    name = "plex_search"
    description = (
        "Search everything in the Plex library at once -- movies, TV shows, music, and "
        "anything else -- for a 'find X' request without knowing which library section it's "
        "in. Each result names which library it came from."
    )
    permission = ToolPermission.READ
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Text to search for, e.g. a title or artist name.",
            },
        },
        "required": ["query"],
    }

    def __init__(self, client: PlexClient) -> None:
        self._client = client

    def run(self, query: str = "", **kwargs: Any) -> Any:
        return self._client.search(query)


class PlexSuggestTool(Tool):
    """What to watch tonight, from your own viewing (7.17): carry on with what you're
    part-way through, new episodes of shows you watch, then unwatched films that were
    added lately, closest to the genres you've watched first."""

    name = "plex_suggest"
    description = (
        "Suggest something to watch on Plex from the user's own viewing: shows they're part-way through, new "
        "episodes of shows they watch, and recently added films they haven't seen that suit their taste. Use for "
        "'what should I watch tonight?', 'anything new to watch?', 'suggest a film'."
    )
    permission = ToolPermission.READ
    parameters = {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": ["any", "movie", "show"], "description": "Films, shows or either. Defaults to any."},
            "count": {"type": "integer", "description": "How many suggestions. Defaults to 5."},
        },
        "required": [],
    }

    def __init__(
        self, client: PlexClient, my_history: Callable[[], list[str]] | None = None, my_watchlist: Callable[[], list[dict]] | None = None
    ) -> None:
        self._client = client
        self._my_history = my_history or (lambda: [])
        self._my_watchlist = my_watchlist or (lambda: [])

    def run(self, kind: str = "any", count: int = 5, **kwargs: Any) -> Any:
        count = max(1, min(int(count or 5), 15))
        on_deck = self._client.on_deck()
        added = self._client.recently_added(limit=60)
        try:
            history = self._my_history()
        except Exception:  # Tautulli down: suggest from Plex alone
            history = []
        shows_i_watch = {t.split(" - ")[0].strip().lower() for t in history if t} | {
            (i.get("show") or "").lower() for i in on_deck if i.get("show")
        }
        out: list[dict[str, Any]] = []
        seen: set[str] = set()

        def add(item: dict[str, Any], why: str) -> None:
            key = (item.get("show") or item.get("title") or "").lower()
            if key and key not in seen and len(out) < count:
                seen.add(key)
                out.append({**item, "why": why})

        for wanted in self._my_watchlist():   # 7.27: your watchlist first, when it's on the server
            try:
                hits = [h for h in self._client.search(wanted["title"]) if (h.get("title") or "").lower() == wanted["title"].lower()]
            except Exception:
                hits = []
            if hits and (kind == "any" or (kind == "movie") == (hits[0].get("type") == "movie")):
                add({"title": hits[0]["title"], "type": hits[0].get("type"), "year": hits[0].get("year")}, "on your watchlist")
        if kind in ("any", "show"):
            for item in on_deck:
                add(item, "you're part-way through it")
            for item in added:
                if item["type"] in ("episode", "season") and not item["watched"] and (item.get("show") or "").lower() in shows_i_watch:
                    add(item, "a new episode of a show you watch")
        if kind in ("any", "movie"):
            if kind == "movie":
                out = [o for o in out if o.get("type") == "movie"]
            liked: dict[str, int] = {}
            for item in added:
                if item["type"] == "movie" and item["watched"]:
                    for genre in item["genres"]:
                        liked[genre] = liked.get(genre, 0) + 1
            films = [i for i in added if i["type"] == "movie" and not i["watched"]]
            films.sort(key=lambda i: (-sum(liked.get(g, 0) for g in i["genres"]), -(i.get("rating") or 0), -(i.get("added_at") or 0)))
            for item in films:
                shared = [g for g in item["genres"] if g in liked]
                add(item, f"new, and you like {shared[0].lower()}" if shared else "new to the server")
        return {"suggestions": out} if out else {"suggestions": [], "note": "Nothing new or part-watched on the server."}


class WatchlistAddTool(Tool):
    name = "watchlist_add"
    description = "Add a film or show to the user's watchlist ('remind me to watch Dune', 'add Severance to my watchlist')."
    permission = ToolPermission.SAFE_ACTION
    parameters = {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "The film or show."},
            "note": {"type": "string", "description": "Optional: why, or who recommended it."},
        },
        "required": ["title"],
    }

    def __init__(self, siris: Any) -> None:
        self._siris = siris

    def run(self, title: str = "", note: str = "", **kwargs: Any) -> Any:
        from app.media import watching

        return watching.add(self._siris, title, note)


class WatchlistTool(Tool):
    name = "watchlist"
    description = "Show the user's watchlist, or remove something from it (remove='Dune')."
    permission = ToolPermission.SAFE_ACTION
    parameters = {
        "type": "object",
        "properties": {"remove": {"type": "string", "description": "A title to take off the list."}},
        "required": [],
    }

    def __init__(self, siris: Any) -> None:
        self._siris = siris

    def run(self, remove: str = "", **kwargs: Any) -> Any:
        from app.media import watching

        if remove.strip():
            return {"removed": watching.remove(self._siris, remove), "watchlist": watching.watchlist(self._siris)}
        return {"watchlist": watching.watchlist(self._siris)}
