"""Meeting prep (milestone 74): for calendar events coming up soon,
surface related Obsidian notes and remembered facts matching the event's
title -- so walking into a meeting doesn't mean starting cold.

Composes existing tools only, same shape as `ServerHealthSkill`/
`DailyBriefingSkill` -- no new client, no new settings. Reuses
`calendar_check_conflicts` (milestone 63) rather than
`calendar_upcoming_events` for the calendar read: the latter's
`days_ahead` is day-granularity ("what's coming up this week"), while
meeting prep needs an hours-out window ("what's coming up soon enough to
prep for"), and `calendar_check_conflicts` already takes an arbitrary
[start, end) window -- the exact same "list events overlapping this
range" query, just used for a different purpose than checking for a
clash.

Related notes/memories are searched by the event's raw `summary` text
(the only descriptive field `CalendarClient.events_between` extracts --
there's no attendee list to search by instead). An event with no summary
gets no search rather than searching on an empty string, which would
match everything and surface noise instead of anything actually related.

Obsidian is optional enrichment (best-effort, same "an unreachable
source shouldn't take the whole result down" precedent
`DailyBriefingSkill` already established for weather/Plex) -- skipped
outright if not configured, and any lookup error for one event just
leaves that event's `related_notes` empty rather than failing the whole
call. Memory recall has no configuration gate (like `siris.goals` in
`DailyBriefingSkill`) so it's always attempted directly.
"""

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from app.skills.base import Skill

if TYPE_CHECKING:
    from app.core.siris import SirisAI

_DEFAULT_HOURS_AHEAD = 4
_MAX_RELATED_NOTES = 3
_MAX_RELATED_MEMORIES = 3


class MeetingPrepSkill(Skill):
    name = "meeting_prep"
    description = (
        "For calendar events coming up in the next few hours, surface related Obsidian notes and "
        "remembered facts matching the event's title. Read-only."
    )
    parameters = {
        "type": "object",
        "properties": {
            "hours_ahead": {
                "type": "integer",
                "description": "How many hours ahead to look for meetings. Defaults to 4.",
            },
        },
        "required": [],
    }

    def run(
        self, siris: "SirisAI", hours_ahead: int = _DEFAULT_HOURS_AHEAD, **kwargs: Any
    ) -> Any:
        if siris.tools.get("calendar_check_conflicts") is None:
            return {"checked": False, "reason": "Calendar is not configured."}

        now = datetime.now(UTC)
        window_end = now + timedelta(hours=hours_ahead)
        events = siris.run_tool(
            "calendar_check_conflicts",
            {"start": now.isoformat(), "end": window_end.isoformat()},
        )

        meetings = [self._prep_for(siris, event) for event in events]
        return {"checked": True, "meetings": meetings}

    @staticmethod
    def _prep_for(siris: "SirisAI", event: dict[str, Any]) -> dict[str, Any]:
        title = event.get("summary") or ""

        related_notes: list[dict[str, Any]] = []
        if title and siris.tools.get("obsidian_search") is not None:
            try:
                related_notes = siris.run_tool("obsidian_search", {"query": title})[
                    :_MAX_RELATED_NOTES
                ]
            except Exception:
                pass

        related_memories = (
            siris.memory.recall(query=title, limit=_MAX_RELATED_MEMORIES)
            if title
            else []
        )

        return {
            "event": event,
            "related_notes": related_notes,
            "related_memories": [m["content"] for m in related_memories],
        }
