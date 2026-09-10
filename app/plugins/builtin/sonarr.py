"""Sonarr tools -- TV library visibility and search triggering, via
app/sonarr/client.py. Registered only when SIRISAI_SONARR_BASE_URL and
SIRISAI_SONARR_API_KEY are both set. Same scope reasoning as Radarr's
tools (app/plugins/builtin/radarr.py) -- READ for listing/queue/lookup/
calendar/root-folders/quality-profiles, SAFE_ACTION for triggering a
search, CONFIRM_REQUIRED for adding/removing a series or a stuck queue
item -- these change what's actually in the library (and delete_series/
remove_queue_item's blocklist option can affect future behavior), unlike
a routine search.

sonarr_delete_series's `dry_run()` (a later hardening pass) reports the
target series' title/year and that `delete_files` would remove its
downloaded episode files -- same "show what's at risk before
confirming a no-undo action" reasoning as Radarr's own
radarr_delete_movie.dry_run(). Unlike a movie, list_series() doesn't
carry a per-series file-presence flag, so this can't say whether there's
actually anything to delete the way radarr_delete_movie's dry_run can --
only that `delete_files=true` would attempt to.
"""

from typing import Any

from app.plugins.base import Tool, ToolPermission, ToolVerification
from app.sonarr.client import SonarrClient


class SonarrListSeriesTool(Tool):
    name = "sonarr_list_series"
    description = (
        "List TV series in the Sonarr library -- title, year, monitored status."
    )
    permission = ToolPermission.READ
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, client: SonarrClient) -> None:
        self._client = client

    def run(self, **kwargs: Any) -> Any:
        return self._client.list_series()


class SonarrQueueTool(Tool):
    name = "sonarr_queue"
    description = "List Sonarr's current download queue -- what's actively downloading and its progress."
    permission = ToolPermission.READ
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, client: SonarrClient) -> None:
        self._client = client

    def run(self, **kwargs: Any) -> Any:
        return self._client.queue()


class SonarrSearchSeriesTool(Tool):
    name = "sonarr_search_series"
    description = (
        "Trigger a background search for every monitored episode of one series already in the "
        "Sonarr library, using its series_id from sonarr_list_series. Queues the search -- "
        "doesn't wait for or guarantee a result."
    )
    permission = ToolPermission.SAFE_ACTION
    parameters = {
        "type": "object",
        "properties": {
            "series_id": {
                "type": "integer",
                "description": "The Sonarr series id to search for.",
            }
        },
        "required": ["series_id"],
    }

    def __init__(self, client: SonarrClient) -> None:
        self._client = client

    def run(self, series_id: int = 0, **kwargs: Any) -> Any:
        return self._client.search_series(series_id)


class SonarrLookupSeriesTool(Tool):
    name = "sonarr_lookup_series"
    description = (
        "Search TVDB (via Sonarr) for a series to add -- not a library search. Use this to find a "
        "series' tvdb_id and title before calling sonarr_add_series."
    )
    permission = ToolPermission.READ
    parameters = {
        "type": "object",
        "properties": {
            "term": {
                "type": "string",
                "description": "Series title to search for, e.g. 'Severance'.",
            }
        },
        "required": ["term"],
    }

    def __init__(self, client: SonarrClient) -> None:
        self._client = client

    def run(self, term: str = "", **kwargs: Any) -> Any:
        return self._client.lookup_series(term)


class SonarrListRootFoldersTool(Tool):
    name = "sonarr_list_root_folders"
    description = (
        "List Sonarr's configured root folders (library paths) and their free space -- use this to "
        "find a valid root_folder_path before calling sonarr_add_series."
    )
    permission = ToolPermission.READ
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, client: SonarrClient) -> None:
        self._client = client

    def run(self, **kwargs: Any) -> Any:
        return self._client.list_root_folders()


class SonarrListQualityProfilesTool(Tool):
    name = "sonarr_list_quality_profiles"
    description = (
        "List Sonarr's configured quality profiles -- use this to find a valid quality_profile_id "
        "before calling sonarr_add_series."
    )
    permission = ToolPermission.READ
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, client: SonarrClient) -> None:
        self._client = client

    def run(self, **kwargs: Any) -> Any:
        return self._client.list_quality_profiles()


class SonarrAddSeriesTool(Tool):
    name = "sonarr_add_series"
    description = (
        "Add a new TV series to the Sonarr library. Use sonarr_lookup_series first for tvdb_id/title, "
        "and sonarr_list_root_folders / sonarr_list_quality_profiles for a valid root_folder_path / "
        "quality_profile_id. Triggers an immediate search for missing episodes by default."
    )
    permission = ToolPermission.CONFIRM_REQUIRED
    parameters = {
        "type": "object",
        "properties": {
            "tvdb_id": {
                "type": "integer",
                "description": "The series' TVDB id, from sonarr_lookup_series.",
            },
            "title": {
                "type": "string",
                "description": "The series' title, from sonarr_lookup_series.",
            },
            "quality_profile_id": {
                "type": "integer",
                "description": "A valid profile id, from sonarr_list_quality_profiles.",
            },
            "root_folder_path": {
                "type": "string",
                "description": "A valid path, from sonarr_list_root_folders.",
            },
            "monitored": {
                "type": "boolean",
                "description": "Whether Sonarr should watch for/download this series. Defaults to true.",
            },
            "search_now": {
                "type": "boolean",
                "description": "Trigger an immediate search for missing episodes after adding. Defaults to true.",
            },
        },
        "required": ["tvdb_id", "title", "quality_profile_id", "root_folder_path"],
    }

    def __init__(self, client: SonarrClient) -> None:
        self._client = client

    def run(
        self,
        tvdb_id: int = 0,
        title: str = "",
        quality_profile_id: int = 0,
        root_folder_path: str = "",
        monitored: bool = True,
        search_now: bool = True,
        **kwargs: Any,
    ) -> Any:
        return self._client.add_series(
            tvdb_id,
            title,
            quality_profile_id,
            root_folder_path,
            monitored=monitored,
            search_now=search_now,
        )

    def verify(self, arguments: dict[str, Any], result: Any) -> ToolVerification:
        """Independent check that the series actually landed in the
        library -- same "the call not raising doesn't mean it happened"
        gap RadarrAddMovieTool.verify() closes for movies."""
        series_id = result.get("id") if isinstance(result, dict) else None
        if not series_id:
            return ToolVerification(
                verified=False,
                detail="Sonarr's add response didn't include a series id.",
            )
        if any(s["id"] == series_id for s in self._client.list_series()):
            return ToolVerification(
                verified=True,
                detail=f"Series id {series_id} now appears in the Sonarr library.",
            )
        return ToolVerification(
            verified=False,
            detail=f"Series id {series_id} does not appear in the Sonarr library.",
        )


class SonarrDeleteSeriesTool(Tool):
    name = "sonarr_delete_series"
    description = (
        "Remove a series from the Sonarr library, using its series_id from sonarr_list_series. "
        "Optionally also deletes the downloaded file(s) from disk."
    )
    permission = ToolPermission.CONFIRM_REQUIRED
    parameters = {
        "type": "object",
        "properties": {
            "series_id": {
                "type": "integer",
                "description": "The Sonarr series id to remove.",
            },
            "delete_files": {
                "type": "boolean",
                "description": "Also delete the downloaded episode file(s) from disk. Defaults to false.",
            },
        },
        "required": ["series_id"],
    }

    def __init__(self, client: SonarrClient) -> None:
        self._client = client

    def run(self, series_id: int = 0, delete_files: bool = False, **kwargs: Any) -> Any:
        return self._client.delete_series(series_id, delete_files=delete_files)

    def dry_run(self, series_id: int = 0, delete_files: bool = False, **kwargs: Any) -> Any:
        """What deleting `series_id` would do, without doing it: the
        series' title/year, and whether `delete_files` would remove its
        downloaded episode files. Deleting a series is no-undo, more so
        with `delete_files=true`, so whoever is about to confirm should
        see exactly what's at risk, not discover it after -- same
        reasoning as RadarrDeleteMovieTool.dry_run()."""
        match = next(
            (s for s in self._client.list_series() if s["id"] == series_id), None
        )
        return {
            "series_id": series_id,
            "title": match["title"] if match else None,
            "year": match["year"] if match else None,
            "would_remove_from_library": True,
            "would_delete_downloaded_files": delete_files,
        }

    def verify(self, arguments: dict[str, Any], result: Any) -> ToolVerification:
        """Mirror of SonarrAddSeriesTool.verify(): a 200 from Sonarr's
        DELETE doesn't prove the series is actually gone. Re-lists the
        library and confirms the series_id no longer appears."""
        series_id = arguments.get("series_id")
        if not series_id:
            raise NotImplementedError(
                f"{self.name} does not support verify without a series_id"
            )

        if any(s["id"] == series_id for s in self._client.list_series()):
            return ToolVerification(
                verified=False,
                detail=f"Series id {series_id} still appears in the Sonarr library.",
            )
        return ToolVerification(
            verified=True,
            detail=f"Series id {series_id} confirmed removed from the Sonarr library.",
        )


class SonarrCalendarTool(Tool):
    name = "sonarr_calendar"
    description = "List upcoming episode air dates Sonarr is tracking."
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

    def __init__(self, client: SonarrClient) -> None:
        self._client = client

    def run(self, days_ahead: int = 7, **kwargs: Any) -> Any:
        return self._client.calendar(days_ahead=days_ahead)


class SonarrRemoveQueueItemTool(Tool):
    name = "sonarr_remove_queue_item"
    description = (
        "Remove one item from Sonarr's download queue (e.g. a stuck or errored download), using "
        "its id from sonarr_queue. Optionally blocklists it so Sonarr won't grab that same release "
        "again."
    )
    permission = ToolPermission.CONFIRM_REQUIRED
    parameters = {
        "type": "object",
        "properties": {
            "queue_id": {
                "type": "integer",
                "description": "The queue item id, from sonarr_queue.",
            },
            "blocklist": {
                "type": "boolean",
                "description": "Also blocklist this release so it isn't grabbed again. Defaults to false.",
            },
        },
        "required": ["queue_id"],
    }

    def __init__(self, client: SonarrClient) -> None:
        self._client = client

    def run(self, queue_id: int = 0, blocklist: bool = False, **kwargs: Any) -> Any:
        return self._client.remove_queue_item(queue_id, blocklist=blocklist)

    def verify(self, arguments: dict[str, Any], result: Any) -> ToolVerification:
        """Same gap as SonarrDeleteSeriesTool.verify(): a 200 from
        Sonarr's DELETE doesn't prove the queue item is actually gone --
        it could be re-added by Sonarr itself (an immediate re-grab when
        not blocklisted) or the removal could silently fail. Re-lists
        the queue and confirms the queue_id no longer appears."""
        queue_id = arguments.get("queue_id")
        if not queue_id:
            raise NotImplementedError(
                f"{self.name} does not support verify without a queue_id"
            )

        if any(item["id"] == queue_id for item in self._client.queue()):
            return ToolVerification(
                verified=False,
                detail=f"Queue item {queue_id} is still present in the queue.",
            )
        return ToolVerification(
            verified=True,
            detail=f"Queue item {queue_id} confirmed removed from the queue.",
        )
