"""Event-timing anomaly detection (a hardening pass): flags a recurring
event as overdue based on its own history -- "the backup normally
finishes by 3am but hasn't today" was milestone 19's original example of
what "proactive" should eventually mean, cut at the time because a fresh
install has no event history to detect a pattern in. Milestone 27 later
built exactly this shape of statistical baseline for metrics
(MetricsStore.baseline()); this applies the same idea to event timing,
now that there's a concrete mechanism (EventMemoryStore.timing_baseline())
to build it on.

Deliberately no explicit "register this as a recurring event" step: any
event signature (description + entity_name) that's occurred at least
`_MIN_SAMPLES_FOR_BASELINE` times among recent history is automatically a
candidate (EventMemoryStore.distinct_recurring_signatures()) -- a one-off
event never accumulates enough occurrences to be checked, so this can't
flag something that was never actually recurring in the first place.

Same shape as ServerHealthSkill (its own docstring says "copy this file's
shape for the next skill") -- a skill composed from existing store
methods, deterministic threshold logic, no fuzzy LLM judgment, so it
composes into POST /siris/proactive/check the same way.
"""

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from app.skills.base import Skill

if TYPE_CHECKING:
    from app.core.siris import SirisAI

_MIN_SAMPLES_FOR_BASELINE = 5
# How many stdevs past the mean interval before "overdue" -- generous on
# purpose (2 stdevs already covers the large majority of normal jitter),
# since this is the sole signal deciding whether to flag something, unlike
# server_health_check's fixed-threshold safety ceiling for metrics.
_STDEV_MULTIPLIER = 2
# A signature whose own occurrences cluster within less than this many
# hours of each other isn't "recurring on a schedule" in the sense this
# check cares about -- it's a burst (several events logged in the same
# minute, e.g. retries or a webhook firing repeatedly), and a burst's
# near-zero mean/stdev makes *any* later gap look "overdue" by orders of
# magnitude, which isn't a real signal. Caught live: 5 events posted back
# to back over the real event bus were flagged overdue a few hundred
# milliseconds later. Below this floor, a signature is simply never
# checked -- not an edge case of the threshold math, a precondition for
# treating it as a schedule at all.
_MIN_MEAN_INTERVAL_HOURS = 1.0


class EventTimingSkill(Skill):
    name = "event_timing_check"
    description = (
        "Check whether any event SirisAI has seen recur regularly (e.g. a nightly "
        "backup completing, a scheduled sync) is overdue based on its own history -- "
        "no fixed schedule needs to be configured, a pattern is learned automatically "
        "once an event has recurred often enough."
    )
    parameters = {"type": "object", "properties": {}, "required": []}

    def run(self, siris: "SirisAI", now: datetime | None = None, **kwargs: Any) -> Any:
        # Same SQLite-round-trip normalization as RoutineStore.due_routines()
        # -- stored timestamps come back naive-UTC, so `now` has to match.
        now = (now or datetime.now(UTC)).replace(tzinfo=None)

        overdue: list[dict[str, Any]] = []
        warnings: list[str] = []
        for description, entity_name in siris.events.distinct_recurring_signatures(min_samples=_MIN_SAMPLES_FOR_BASELINE):
            baseline = siris.events.timing_baseline(description, entity_name, min_samples=_MIN_SAMPLES_FOR_BASELINE)
            if baseline is None or baseline.mean_interval_hours < _MIN_MEAN_INTERVAL_HOURS:
                continue

            hours_since = (now - baseline.last_occurred_at).total_seconds() / 3600
            threshold_hours = baseline.mean_interval_hours + _STDEV_MULTIPLIER * baseline.stdev_interval_hours
            if hours_since <= threshold_hours:
                continue

            label = f"'{description}'" + (f" ({entity_name})" if entity_name else "")
            message = (
                f"Expected recurring event is overdue: {label} -- last seen {hours_since:.1f}h ago, "
                f"usually every {baseline.mean_interval_hours:.1f}h ± {baseline.stdev_interval_hours:.1f}h "
                f"({baseline.sample_count} occurrences)"
            )
            warnings.append(message)
            overdue.append(
                {
                    "description": description,
                    "entity_name": entity_name,
                    "hours_since_last": round(hours_since, 1),
                    "mean_interval_hours": round(baseline.mean_interval_hours, 1),
                    "stdev_interval_hours": round(baseline.stdev_interval_hours, 1),
                    "sample_count": baseline.sample_count,
                }
            )

        return {"overdue_events": overdue, "warnings": warnings, "healthy": not warnings}
