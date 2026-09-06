"""Morning/evening briefing (milestone 39): composes what already exists
-- calendar, weather, the proactive health/timing checks, and active
goals -- into one digest. Same shape as `ServerHealthSkill` (its own
docstring: "copy this file's shape for the next skill"): a skill that
calls existing tools through `siris.run_tool()`, no new subsystem.

Read-only and side-effect-free for everything except queued
notifications (below) -- this composes and summarizes, it doesn't decide
whether to *push* anything -- that's `POST /siris/proactive/briefing`'s
job (app/api/routes/proactive.py), same division of responsibility
milestone 19 already drew between `server_health_check` and
`POST /siris/proactive/check`.

"Goals due soon" was the original brainstorm framing, but `Goal`
(app/db/models.py) has no due-date field -- this surfaces what's still
*active* instead, an honest match for what the data actually supports
rather than inventing a due-date concept this milestone doesn't need.

Recent Plex activity (milestone 53) is the same idea applied to Tautulli:
the original brainstorm framing was "overnight activity," but
`tautulli_recent_history`'s `date` field is whatever Tautulli's API
hands back (see app/tautulli/client.py -- never parsed into a real
datetime anywhere in this codebase), so filtering to an "overnight"
window would mean inventing a time-parsing rule this milestone can't
actually verify against a real instance. Surfacing the last few plays
as-is, same "recent" framing `tautulli_recent_history` already uses, is
the honest match for what the data supports.

Queued notifications (milestone 72, an interruption controller): POST
/siris/proactive/check no longer pushes "notify"-tier warnings
immediately, it queues them (app/notify/pending.py) instead -- this is
where they're actually delivered, folded into the digest rather than
interrupting. Read AND cleared here (not just read): once a briefing has
carried them, leaving them queued would mean the next briefing repeats
stale warnings that already reached the user. `queued_notifications`
still lists what was cleared, so the response is honest about what this
particular briefing surfaced.
"""

from typing import TYPE_CHECKING, Any

from app.skills.base import Skill

if TYPE_CHECKING:
    from app.core.siris import SirisAI


class DailyBriefingSkill(Skill):
    name = "daily_briefing"
    description = (
        "Compose a summary of the day: weather, upcoming calendar events, recent Plex activity, any "
        "health or recurring-event warnings, queued non-urgent notifications, and active goals. "
        "Doesn't push/speak anything itself; POST /siris/proactive/briefing does that, this just "
        "builds the content -- but it does clear the queued-notification queue once read."
    )
    parameters = {"type": "object", "properties": {}, "required": []}

    def run(self, siris: "SirisAI", **kwargs: Any) -> Any:
        briefing: dict[str, Any] = {}

        # Weather/calendar are enrichment, not the point of the briefing --
        # an unreachable weather API or CalDAV server shouldn't take the
        # whole briefing down with it, same "best-effort" precedent as
        # DockerRestartContainerTool.dry_run()'s Plex session check. Caught
        # live: the first pass let a real network failure here 500 the
        # entire endpoint, losing warnings/goals that had nothing to do
        # with weather.
        if siris.tools.get("weather_current") is not None:
            try:
                briefing["weather"] = siris.run_tool("weather_current", {})
            except Exception:
                pass

        if siris.tools.get("calendar_upcoming_events") is not None:
            try:
                briefing["calendar"] = siris.run_tool(
                    "calendar_upcoming_events", {"days_ahead": 1}
                )
            except Exception:
                pass

        if siris.tools.get("tautulli_recent_history") is not None:
            try:
                briefing["plex_recent_activity"] = siris.run_tool(
                    "tautulli_recent_history", {"length": 5}
                )
            except Exception:
                pass

        health = siris.run_tool("server_health_check", {})
        event_timing = siris.run_tool("event_timing_check", {})
        briefing["warnings"] = health["warnings"] + event_timing["warnings"]

        queued = siris.pending_notifications.list_pending()
        briefing["queued_notifications"] = [item["message"] for item in queued]
        if queued:
            siris.pending_notifications.clear()

        briefing["active_goals"] = [
            g["title"] for g in siris.goals.list_goals(status="active")
        ]
        briefing["summary"] = self._compose_summary(briefing)
        return briefing

    @staticmethod
    def _compose_summary(briefing: dict[str, Any]) -> str:
        lines: list[str] = []

        weather = briefing.get("weather")
        if weather is not None:
            lines.append(
                f"Weather: {weather['temperature_c']}°C, {weather['conditions']}."
            )

        calendar = briefing.get("calendar")
        if calendar is not None:
            if calendar:
                events = "; ".join(f"{e['summary']} ({e['start']})" for e in calendar)
                lines.append(f"Today: {events}.")
            else:
                lines.append("Nothing on the calendar today.")

        plex_activity = briefing.get("plex_recent_activity")
        if plex_activity is not None:
            if plex_activity:
                watched = "; ".join(
                    f"{a['title']} ({a['user']})" for a in plex_activity
                )
                lines.append(f"Recently watched: {watched}.")
            else:
                lines.append("Nothing recently watched.")

        warnings = briefing["warnings"]
        lines.append(
            f"{len(warnings)} warning(s): {'; '.join(warnings)}"
            if warnings
            else "All systems normal."
        )

        queued_notifications = briefing["queued_notifications"]
        if queued_notifications:
            lines.append(f"Also since last time: {'; '.join(queued_notifications)}.")

        goals = briefing["active_goals"]
        lines.append(
            f"Active goals ({len(goals)}): {', '.join(goals)}."
            if goals
            else "No active goals."
        )

        return " ".join(lines)
