"""Event reaction (a hardening pass): flags newly-logged events
(`POST /siris/events`, app/api/routes/events.py) whose description looks
like something broke, composed into the same `POST /siris/proactive/check`
pipeline as server_health_check/event_timing_check/goal_check_in/
new_device_alert.

app/api/routes/events.py's own module docstring left "deciding whether to
ignore, investigate, or proactively notify based on an incoming event" as
a later milestone deliberately: unlike every other proactive skill here,
an incoming event can come from *any* external system with a completely
freeform description -- there's no fixed message shape
NotificationManager (app/notify/manager.py) can pattern-match the way it
already does for server_health_check's "Container 'X' is exited". Asked
directly how to close that gap -- a deterministic keyword scan (consistent
with every other proactive skill's "simple keyword/pattern matching, not
fuzzy judgment" design, per NotificationManager's own module docstring)
versus an LLM judgment call on each event -- the answer was the
deterministic keyword scan.

Deliberately a small, fixed keyword list, not a generic classifier: the
common "something broke" vocabulary ("error", "failed", "failure",
"critical", "offline", "unreachable", "down"), matched case-insensitively
against the event's own description. An event phrased without any of
these words goes unflagged -- a real, accepted false-negative rate, not
something worth chasing with an ever-growing keyword list that still
couldn't cover every external system's own vocabulary.

A monotonic cursor (EventMemoryStore.get_reaction_cursor/
advance_reaction_cursor) tracks the highest event id already classified,
the same "seen set persists across restarts" reasoning KnownNetworkDevice
uses for new_device_alert -- otherwise every POST /siris/proactive/check
run on a schedule would re-flag the same old event forever.

Cross-domain correlation (a Jarvis-concept brainstorm item): a flagged
event's message now ends with " (entity: X)" when the event was logged
against a named entity (Event.entity_name) -- the same
"({label}) is high"/"Container 'X' is..." named-entity shape
NotificationManager's own correlation already keys off of, so a flagged
event about "NAS" can now correlate with an unrelated but genuinely
related warning about the same entity (a disk warning for NAS's storage,
a container that serves it) via the existing world-model relationships,
the same way a Container warning already correlates with a Disk one --
see NotificationManager's own module docstring for how.
"""

from typing import TYPE_CHECKING, Any

from app.skills.base import Skill

if TYPE_CHECKING:
    from app.core.siris import SirisAI

_FLAG_KEYWORDS = (
    "error",
    "failed",
    "failure",
    "critical",
    "offline",
    "unreachable",
    "down",
)


class EventReactionSkill(Skill):
    name = "event_reaction_check"
    description = (
        "Check events logged via POST /siris/events since the last check for ones whose "
        "description looks like something broke (a small fixed keyword scan), and warn about "
        "any found. Read-only -- doesn't send anything on its own; POST /siris/proactive/check "
        "does that, this just detects."
    )
    parameters = {"type": "object", "properties": {}, "required": []}

    def run(self, siris: "SirisAI", **kwargs: Any) -> Any:
        last_id = siris.events.get_reaction_cursor()
        new_events = siris.events.events_since(last_id)
        if not new_events:
            return {"checked": True, "flagged": [], "warnings": []}

        flagged = [
            event
            for event in new_events
            if any(keyword in event["description"].lower() for keyword in _FLAG_KEYWORDS)
        ]
        siris.events.advance_reaction_cursor(new_events[-1]["id"])

        return {
            "checked": True,
            "flagged": [event["id"] for event in flagged],
            "warnings": [
                f"Event flagged: {event['description']}"
                + (f" (source: {event['source']})" if event["source"] else "")
                + (f" (entity: {event['entity_name']})" if event["entity_name"] else "")
                for event in flagged
            ],
        }
