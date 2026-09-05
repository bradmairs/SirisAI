"""Radarr tools -- movie library visibility and search triggering, via
app/radarr/client.py. Registered only when SIRISAI_RADARR_BASE_URL and
SIRISAI_RADARR_API_KEY are both set.

Listing/queue/lookup/calendar/root-folders/quality-profiles are READ,
triggering a search is SAFE_ACTION (routine and reversible -- it just
queues a background search, doesn't remove or overwrite anything).

Adding/deleting a movie (a hardening pass) is the "bigger, more
consequential action" this file's docstring used to defer -- now built,
CONFIRM_REQUIRED: unlike a search, these change what's actually in the
library (and delete_movie can remove files from disk), so they get the
same human-in-the-loop treatment as HomeAssistantCallServiceTool by
default, not SAFE_ACTION's "runs immediately" treatment. Removing a
stuck/errored queue item (a later hardening pass) is CONFIRM_REQUIRED for
the same reason -- it's a real removal, optionally with a blocklist that
changes future behavior, not just a status query.
"""

from typing import Any

from app.plugins.base import Tool, ToolPermission, ToolVerification
from app.radarr.client import RadarrClient


class RadarrListMoviesTool(Tool):
    name = "radarr_list_movies"
    description = (
        "List movies in the Radarr library -- title, year, monitored/download status."
    )
    permission = ToolPermission.READ
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, client: RadarrClient) -> None:
        self._client = client

    def run(self, **kwargs: Any) -> Any:
        return self._client.list_movies()


class RadarrQueueTool(Tool):
    name = "radarr_queue"
    description = "List Radarr's current download queue -- what's actively downloading and its progress."
    permission = ToolPermission.READ
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, client: RadarrClient) -> None:
        self._client = client

    def run(self, **kwargs: Any) -> Any:
        return self._client.queue()


class RadarrSearchMovieTool(Tool):
    name = "radarr_search_movie"
    description = (
        "Trigger a background search for one movie already in the Radarr library, using its "
        "movie_id from radarr_list_movies. Queues the search -- doesn't wait for or guarantee a result."
    )
    permission = ToolPermission.SAFE_ACTION
    parameters = {
        "type": "object",
        "properties": {
            "movie_id": {
                "type": "integer",
                "description": "The Radarr movie id to search for.",
            }
        },
        "required": ["movie_id"],
    }

    def __init__(self, client: RadarrClient) -> None:
        self._client = client

    def run(self, movie_id: int = 0, **kwargs: Any) -> Any:
        return self._client.search_movie(movie_id)


class RadarrLookupMovieTool(Tool):
    name = "radarr_lookup_movie"
    description = (
        "Search TMDB (via Radarr) for a movie to add -- not a library search. Use this to find a "
        "movie's tmdb_id and title before calling radarr_add_movie."
    )
    permission = ToolPermission.READ
    parameters = {
        "type": "object",
        "properties": {
            "term": {
                "type": "string",
                "description": "Movie title to search for, e.g. 'Dune 2021'.",
            }
        },
        "required": ["term"],
    }

    def __init__(self, client: RadarrClient) -> None:
        self._client = client

    def run(self, term: str = "", **kwargs: Any) -> Any:
        return self._client.lookup_movie(term)


class RadarrListRootFoldersTool(Tool):
    name = "radarr_list_root_folders"
    description = (
        "List Radarr's configured root folders (library paths) and their free space -- use this to "
        "find a valid root_folder_path before calling radarr_add_movie."
    )
    permission = ToolPermission.READ
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, client: RadarrClient) -> None:
        self._client = client

    def run(self, **kwargs: Any) -> Any:
        return self._client.list_root_folders()


class RadarrListQualityProfilesTool(Tool):
    name = "radarr_list_quality_profiles"
    description = (
        "List Radarr's configured quality profiles -- use this to find a valid quality_profile_id "
        "before calling radarr_add_movie."
    )
    permission = ToolPermission.READ
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, client: RadarrClient) -> None:
        self._client = client

    def run(self, **kwargs: Any) -> Any:
        return self._client.list_quality_profiles()


class RadarrAddMovieTool(Tool):
    name = "radarr_add_movie"
    description = (
        "Add a new movie to the Radarr library. Use radarr_lookup_movie first for tmdb_id/title, "
        "and radarr_list_root_folders / radarr_list_quality_profiles for a valid root_folder_path / "
        "quality_profile_id. Triggers an immediate search by default."
    )
    permission = ToolPermission.CONFIRM_REQUIRED
    parameters = {
        "type": "object",
        "properties": {
            "tmdb_id": {
                "type": "integer",
                "description": "The movie's TMDB id, from radarr_lookup_movie.",
            },
            "title": {
                "type": "string",
                "description": "The movie's title, from radarr_lookup_movie.",
            },
            "quality_profile_id": {
                "type": "integer",
                "description": "A valid profile id, from radarr_list_quality_profiles.",
            },
            "root_folder_path": {
                "type": "string",
                "description": "A valid path, from radarr_list_root_folders.",
            },
            "monitored": {
                "type": "boolean",
                "description": "Whether Radarr should watch for/download this movie. Defaults to true.",
            },
            "search_now": {
                "type": "boolean",
                "description": "Trigger an immediate search after adding. Defaults to true.",
            },
        },
        "required": ["tmdb_id", "title", "quality_profile_id", "root_folder_path"],
    }

    def __init__(self, client: RadarrClient) -> None:
        self._client = client

    def run(
        self,
        tmdb_id: int = 0,
        title: str = "",
        quality_profile_id: int = 0,
        root_folder_path: str = "",
        monitored: bool = True,
        search_now: bool = True,
        **kwargs: Any,
    ) -> Any:
        return self._client.add_movie(
            tmdb_id,
            title,
            quality_profile_id,
            root_folder_path,
            monitored=monitored,
            search_now=search_now,
        )

    def verify(self, arguments: dict[str, Any], result: Any) -> ToolVerification:
        """Independent check that the movie actually landed in the
        library, not just that Radarr's own POST returned success -- the
        same "the call not raising doesn't mean it happened" gap this
        tool library's other verify()s close (DockerRestartContainerTool,
        HomeAssistantCallServiceTool, CalendarCreateEventTool): a
        malformed root folder or a profile Radarr silently can't apply
        can leave the add half-done without ever surfacing as an error."""
        movie_id = result.get("id") if isinstance(result, dict) else None
        if not movie_id:
            return ToolVerification(
                verified=False,
                detail="Radarr's add response didn't include a movie id.",
            )
        if any(m["id"] == movie_id for m in self._client.list_movies()):
            return ToolVerification(
                verified=True,
                detail=f"Movie id {movie_id} now appears in the Radarr library.",
            )
        return ToolVerification(
            verified=False,
            detail=f"Movie id {movie_id} does not appear in the Radarr library.",
        )


class RadarrDeleteMovieTool(Tool):
    name = "radarr_delete_movie"
    description = (
        "Remove a movie from the Radarr library, using its movie_id from radarr_list_movies. "
        "Optionally also deletes the downloaded file(s) from disk."
    )
    permission = ToolPermission.CONFIRM_REQUIRED
    parameters = {
        "type": "object",
        "properties": {
            "movie_id": {
                "type": "integer",
                "description": "The Radarr movie id to remove.",
            },
            "delete_files": {
                "type": "boolean",
                "description": "Also delete the downloaded movie file(s) from disk. Defaults to false.",
            },
        },
        "required": ["movie_id"],
    }

    def __init__(self, client: RadarrClient) -> None:
        self._client = client

    def run(self, movie_id: int = 0, delete_files: bool = False, **kwargs: Any) -> Any:
        return self._client.delete_movie(movie_id, delete_files=delete_files)


class RadarrCalendarTool(Tool):
    name = "radarr_calendar"
    description = "List upcoming movie releases Radarr is tracking (cinema/physical/digital release dates)."
    permission = ToolPermission.READ
    parameters = {
        "type": "object",
        "properties": {
            "days_ahead": {
                "type": "integer",
                "description": "How many days ahead to look. Defaults to 7.",
            },
        },
        "required": [],
    }

    def __init__(self, client: RadarrClient) -> None:
        self._client = client

    def run(self, days_ahead: int = 7, **kwargs: Any) -> Any:
        return self._client.calendar(days_ahead=days_ahead)


class RadarrRemoveQueueItemTool(Tool):
    name = "radarr_remove_queue_item"
    description = (
        "Remove one item from Radarr's download queue (e.g. a stuck or errored download), using "
        "its id from radarr_queue. Optionally blocklists it so Radarr won't grab that same release "
        "again."
    )
    permission = ToolPermission.CONFIRM_REQUIRED
    parameters = {
        "type": "object",
        "properties": {
            "queue_id": {
                "type": "integer",
                "description": "The queue item id, from radarr_queue.",
            },
            "blocklist": {
                "type": "boolean",
                "description": "Also blocklist this release so it isn't grabbed again. Defaults to false.",
            },
        },
        "required": ["queue_id"],
    }

    def __init__(self, client: RadarrClient) -> None:
        self._client = client

    def run(self, queue_id: int = 0, blocklist: bool = False, **kwargs: Any) -> Any:
        return self._client.remove_queue_item(queue_id, blocklist=blocklist)
