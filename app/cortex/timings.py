"""How long each part of a chat turn takes (SirisAI 4.6), so speed work
starts from numbers: gathering context, choosing tools, the model's first
call, running the tool, writing the answer, the first word a streaming
client saw, and the whole turn.

Each finished turn records its stages as metrics ("turn_<stage>_ms"), and
the last few turns are kept in memory with their breakdown.
GET /siris/timings returns the typical (median) and slow (95th percentile)
time per stage, plus those recent turns.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.core.siris import SirisAI

STAGES = ("context", "tools", "model", "tool", "answer", "first_word", "total")
_RECENT: deque[dict[str, Any]] = deque(maxlen=50)


class TurnTimer:
    def __init__(self) -> None:
        self.started = time.perf_counter()
        self.stages: dict[str, float] = {}
        self.path = "model"

    @contextmanager
    def stage(self, name: str) -> Iterator[None]:
        began = time.perf_counter()
        try:
            yield
        finally:
            self.stages[name] = self.stages.get(name, 0.0) + (time.perf_counter() - began) * 1000

    def mark(self, name: str) -> None:
        """Time since the turn started, once (the first word, say)."""
        self.stages.setdefault(name, (time.perf_counter() - self.started) * 1000)

    def finish(self) -> dict[str, float]:
        self.stages["total"] = (time.perf_counter() - self.started) * 1000
        return {k: round(v, 1) for k, v in self.stages.items()}


def record(siris: "SirisAI", timer: TurnTimer) -> dict[str, float]:
    stages = timer.finish()
    try:
        siris.metrics.record_samples({f"turn_{k}_ms": v for k, v in stages.items()})
    except Exception:  # noqa: BLE001 - timings must never break a turn
        pass
    _RECENT.appendleft({"at": datetime.now(UTC).isoformat(), "path": timer.path, "stages": stages})
    return stages


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(pct / 100 * (len(ordered) - 1))))
    return round(ordered[index], 1)


def summary(siris: "SirisAI", limit: int = 200) -> dict[str, Any]:
    stages = {}
    for name in STAGES:
        values = siris.metrics.recent_values(f"turn_{name}_ms", limit=limit)
        if values:
            stages[name] = {"p50_ms": _percentile(values, 50), "p95_ms": _percentile(values, 95), "count": len(values)}
    return {"stages": stages, "recent": list(_RECENT)[:20]}
