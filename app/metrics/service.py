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
"""

import statistics
from dataclasses import dataclass

from sqlalchemy import select
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
