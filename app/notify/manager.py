"""Notification intelligence (milestone 26): scores each warning
server_health_check (15) produces and decides what to do with it --
"urgent" / "notify" / "log_only" -- instead of the old flat "unhealthy ->
send one message joining every warning" logic in
POST /siris/proactive/check. Also correlates related warnings into one
message using the world model (21): if a container that's down has a
recorded relationship (remember_entity_relationship) pointing at
something else also flagged -- e.g. a disk usage warning for the storage
it depends on -- they're merged into one message explaining the likely
link instead of surfacing as two separate, seemingly unrelated warnings.

Classification here is deliberately simple keyword/pattern matching
against known message shapes ("CPU usage is high: ...", "Container 'X'
is exited", "Expected recurring event is overdue: ...", "Goal 'X' hasn't
been updated ...", ...) -- server_health_check's (15) and, since later
hardening passes, event_timing_check's and goal_check_in's -- not a
general NLP/anomaly detection system; confidence is always 1.0 because
every warning reaching here already cleared its own deterministic
threshold check, so nothing here ever lands on "ignore" either. A future
source with genuine uncertainty (an LLM's own assessment) would populate
a lower confidence and could legitimately use the fuller
ignore/log_only/notify/urgent range this milestone's action field
supports even though these skills only ever produce the top two.

Correlation is exact-name matching against the container name and disk
path server_health_check's own messages already contain -- it doesn't
try to fuzzy-match "Plex" against a relationship recorded as "plex". A
recorded relationship only helps if it uses the same names these
messages do.
"""

import re
from dataclasses import dataclass, field

from app.memory.entities import EntityMemoryStore

_CONTAINER_PATTERN = re.compile(r"Container '(.+?)' is")
_DISK_PATH_PATTERN = re.compile(r"Disk usage on (.+?) is high")


@dataclass
class NotificationItem:
    message: str
    severity: str  # "critical" | "warning"
    category: str  # "resource" | "docker" | "correlated"
    confidence: float
    action: str  # "urgent" | "notify" | "log_only" | "ignore"
    correlated_with: list[str] = field(default_factory=list)


class NotificationManager:
    def __init__(self, entities: EntityMemoryStore) -> None:
        self._entities = entities

    def summarize(self, warnings: list[str]) -> list[NotificationItem]:
        return self._correlate([self._classify(warning) for warning in warnings])

    def _classify(self, warning: str) -> NotificationItem:
        is_docker = warning.startswith("Container ")
        is_resource = warning.startswith(("CPU usage", "Memory usage", "Disk usage"))
        is_timing = warning.startswith("Expected recurring event is overdue")
        is_goal = warning.startswith("Goal '")
        category = (
            "docker"
            if is_docker
            else "resource"
            if is_resource
            else "timing"
            if is_timing
            else "goals"
            if is_goal
            else "other"
        )

        critical = is_docker and any(state in warning for state in ("exited", "dead"))
        severity = "critical" if critical else "warning"

        return NotificationItem(
            message=warning,
            severity=severity,
            category=category,
            confidence=1.0,
            action="urgent" if severity == "critical" else "notify",
        )

    def _correlate(self, items: list[NotificationItem]) -> list[NotificationItem]:
        merged_indices: set[int] = set()
        result: list[NotificationItem] = []

        for i, item in enumerate(items):
            if i in merged_indices:
                continue

            container = _CONTAINER_PATTERN.search(item.message)
            if container is None:
                result.append(item)
                continue

            related_entities = self._related_entity_names(container.group(1))
            partner = self._find_disk_partner(items, related_entities, skip={i, *merged_indices})

            if partner is None:
                result.append(item)
                continue

            partner_index, partner_item = partner
            merged_indices.add(partner_index)
            result.append(
                NotificationItem(
                    message=f"{item.message} -- likely related to: {partner_item.message}",
                    severity="critical" if item.severity == "critical" else partner_item.severity,
                    category="correlated",
                    confidence=min(item.confidence, partner_item.confidence),
                    action="urgent" if item.action == "urgent" else partner_item.action,
                    correlated_with=[partner_item.message],
                )
            )

        return result

    def _related_entity_names(self, container: str) -> set[str]:
        edges = self._entities.relationships_for(container)
        return {edge["object_entity"] for edge in edges["outgoing"]} | {
            edge["subject_entity"] for edge in edges["incoming"]
        }

    @staticmethod
    def _find_disk_partner(
        items: list[NotificationItem], related_entities: set[str], skip: set[int]
    ) -> tuple[int, NotificationItem] | None:
        for j, other in enumerate(items):
            if j in skip:
                continue
            disk_path = _DISK_PATH_PATTERN.search(other.message)
            if disk_path is not None and disk_path.group(1) in related_entities:
                return j, other
        return None
