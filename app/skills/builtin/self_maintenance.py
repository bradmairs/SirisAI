"""Self-inspection skill (milestone 30): reports on SirisAI itself rather
than the server it runs on -- unused/duplicate memories, tools failing
often, and (the standout capability the roadmap called out) tool-call
sequences the planner (24) has repeated often enough that a reusable
skill would probably be worth writing. Same Skill pattern as
server_health_check (15).

Deliberately data-backed rather than fuzzy: every finding here comes from
a concrete count over stored history (memory recall_count, audit log
errors/sequences) or, for duplicate detection, embedding cosine similarity
(see MemoryStore.find_duplicate_groups()) -- not an LLM's impression of
what seems off.
"""

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from app.skills.base import Skill

if TYPE_CHECKING:
    from app.core.siris import SirisAI

# A memory younger than this never counts as "unused" -- it hasn't had a
# real chance to come up in conversation yet. Arbitrary but documented,
# same as this project's other threshold constants (e.g. milestone 27's
# stdev multiplier).
_UNUSED_MEMORY_MIN_AGE_DAYS = 7
_REPEATED_SEQUENCE_MIN_OCCURRENCES = 3


def _older_than(iso_timestamp: str, min_age: timedelta) -> bool:
    created = datetime.fromisoformat(iso_timestamp)
    if created.tzinfo is None:
        # SQLite drops tzinfo on the way back out -- every stored
        # timestamp in this codebase is UTC by convention (datetime.now(UTC)
        # at write time), so a naive value read back still means UTC.
        created = created.replace(tzinfo=UTC)
    return datetime.now(UTC) - created >= min_age


class SelfMaintenanceSkill(Skill):
    name = "self_maintenance_check"
    description = (
        "Inspect SirisAI's own stored data for maintenance signals: memories that have "
        "never been recalled, duplicate memories, tools that have been failing, and "
        "multi-step tool sequences the planner has repeated often enough to be worth "
        "turning into a reusable skill."
    )
    parameters = {"type": "object", "properties": {}, "required": []}

    def run(self, siris: "SirisAI", **kwargs: Any) -> Any:
        memories = siris.memory.list_all()

        unused_memories = [
            m
            for m in memories
            if m["recall_count"] == 0 and _older_than(m["created_at"], timedelta(days=_UNUSED_MEMORY_MIN_AGE_DAYS))
        ]

        duplicate_memories = siris.memory.find_duplicate_groups()

        failing_tools = siris.audit.failure_counts_by_tool()
        repeated_sequences = siris.audit.repeated_planner_sequences(
            min_occurrences=_REPEATED_SEQUENCE_MIN_OCCURRENCES
        )
        skill_suggestions = [
            f"'{' -> '.join(seq['actions'])}' has run as a plan {seq['occurrences']} times -- "
            "consider turning it into a reusable skill."
            for seq in repeated_sequences
        ]

        return {
            "unused_memories": unused_memories,
            "duplicate_memories": duplicate_memories,
            "failing_tools": failing_tools,
            "repeated_planner_sequences": repeated_sequences,
            "skill_suggestions": skill_suggestions,
        }
