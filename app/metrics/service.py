"""Metric sample history and personalized baselines (milestone 27).

ServerHealthSkill (15) hard-codes CPU/memory/disk >90% as "high" -- this
lets it instead flag a value as unusual for *this* server, learned from
its own recent readings, without abandoning the fixed threshold as a
safety ceiling. Same "own short-lived session per call" pattern as
MemoryStore/EntityMemoryStore.

Samples only accumulate when something actually calls record_sample() --
there's no in-process scheduler taking readings on its own (milestone
19's boundary), so this naturally piggybacks on whatever already calls
server_health_check (a chat turn, POST /siris/proactive/check on an
external cron). A fresh install has no history, so baseline() returns
None until min_samples samples exist -- callers should fall back to a
fixed threshold until then, same honest scope-cut as milestone 19: this
needs real accumulated data to be meaningfully different from a fixed
number, and doesn't pretend day-one readings mean anything.

Time-of-day anomaly detection (a Jarvis-concept brainstorm item):
`hourly_baseline()` is `baseline()`'s same mean/stdev, narrowed to
samples recorded in one specific hour-of-day (UTC) -- "unusual for 3am"
rather than "unusual ever," since a flat baseline across a server's full
day/night cycle can hide a real anomaly (e.g. CPU idling near 0% every
night, then spiking to 40% one night) inside noise that would only ever
flag something above its *overall* mean + N*stdev, which day/night swings
already push high. A finer-grained baseline is a smaller slice of the
same history, so it takes proportionally longer to accumulate
`min_samples` for a given hour than the flat one does -- an honest,
accepted tradeoff for the more specific answer, not hidden by loosening
what counts as "enough."
"""

import statistics
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app.db.models import MetricSample


@dataclass
class Baseline:
    mean: float
    stdev: float
    sample_count: int


class MetricsStore:
    def __init__(self, session_factory: sessionmaker) -> None:
        self._session_factory = session_factory

    def record_sample(self, metric_name: str, value: float) -> None:
        with self._session_factory() as session:
            session.add(MetricSample(metric_name=metric_name, value=value))
            session.commit()

    def record_samples(self, samples: dict[str, float]) -> None:
        """Same as calling record_sample() once per entry, but one session/
        commit for all of them -- MeteredLLMClient (a performance
        hardening pass) records up to three token-count metrics per LLM
        call, on the response path of every chat turn; three separate
        round trips there was three times the DB latency this needed."""
        if not samples:
            return
        with self._session_factory() as session:
            session.add_all([MetricSample(metric_name=name, value=value) for name, value in samples.items()])
            session.commit()

    def recent_values(self, metric_name: str, limit: int = 100) -> list[float]:
        with self._session_factory() as session:
            stmt = (
                select(MetricSample.value)
                .where(MetricSample.metric_name == metric_name)
                .order_by(MetricSample.id.desc())
                .limit(limit)
            )
            return list(session.scalars(stmt).all())

    def baseline(self, metric_name: str, min_samples: int = 10, limit: int = 100) -> Baseline | None:
        """Mean/stdev over the most recent `limit` samples, or None if
        fewer than `min_samples` exist -- not enough history yet for a
        personalized threshold to mean anything over a fixed fallback."""
        values = self.recent_values(metric_name, limit=limit)
        if len(values) < min_samples:
            return None
        stdev = statistics.stdev(values) if len(values) > 1 else 0.0
        return Baseline(mean=statistics.mean(values), stdev=stdev, sample_count=len(values))

    def hourly_baseline(
        self, metric_name: str, hour: int, min_samples: int = 5, limit: int = 2000
    ) -> Baseline | None:
        """`baseline()`'s same mean/stdev, narrowed to samples recorded
        during `hour` (0-23, UTC) among the most recent `limit` samples
        overall -- `limit` is deliberately larger than `baseline()`'s
        default since only roughly 1/24th of a same-sized recent window
        falls in any one hour. None until `min_samples` same-hour
        samples exist, same honest scope-cut as `baseline()` -- a
        fresh install (or a metric that's only just started being
        recorded) has no meaningful per-hour history yet either."""
        with self._session_factory() as session:
            stmt = (
                select(MetricSample.value, MetricSample.created_at)
                .where(MetricSample.metric_name == metric_name)
                .order_by(MetricSample.id.desc())
                .limit(limit)
            )
            rows = session.execute(stmt).all()

        values = [value for value, created_at in rows if created_at.hour == hour]
        if len(values) < min_samples:
            return None
        stdev = statistics.stdev(values) if len(values) > 1 else 0.0
        return Baseline(mean=statistics.mean(values), stdev=stdev, sample_count=len(values))

    def latest_value(self, metric_name: str) -> float | None:
        values = self.recent_values(metric_name, limit=1)
        return values[0] if values else None

    def total(self, metric_name: str) -> float:
        """Sum of every recorded sample -- for a counter-shaped metric
        (e.g. milestone 47's llm_tokens_* -- one sample per LLM call,
        summing to cumulative usage), unlike baseline()'s recent-window
        mean/stdev, which suits a gauge-shaped metric (e.g. cpu_percent,
        power_draw_watts) instead."""
        with self._session_factory() as session:
            stmt = select(func.sum(MetricSample.value)).where(MetricSample.metric_name == metric_name)
            return session.scalar(stmt) or 0.0

    def sample_count(self, metric_name: str) -> int:
        with self._session_factory() as session:
            stmt = select(func.count()).select_from(MetricSample).where(MetricSample.metric_name == metric_name)
            return session.scalar(stmt) or 0
