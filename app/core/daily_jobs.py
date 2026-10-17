"""Jobs that run once a day (or week, or month) at a set local hour: the
nightly reflection, the weekly goal review, and the upkeep jobs that keep
SirisAI running without anyone tending it (app/upkeep/: backups, the
restore drill, housekeeping, update checks, model trials).

Each job runs on the first scheduler tick at or after its hour, on a day it
hasn't already run, with the last run date kept in app_state so a restart
neither skips nor repeats it. One job failing never stops the others; the
outcome of each is kept for `GET /siris/learning/jobs`.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from app.core import clock

if TYPE_CHECKING:
    from app.core.siris import SirisAI

logger = logging.getLogger(__name__)

STATE_KEY = "daily_jobs_last_run"
RESULTS_KEY = "daily_jobs_results"


@dataclass(frozen=True)
class DailyJob:
    name: str
    hour: Callable[["SirisAI"], int | None]  # local hour, None = off
    run: Callable[["SirisAI", datetime], Any]
    weekday: Callable[["SirisAI"], int | None] = lambda s: None  # 0 = Monday; None = every day
    monthday: int | None = None  # e.g. 1 = the first of the month


def _reflect(siris: "SirisAI", now: datetime) -> Any:
    from app.learning.reflection import reflect

    return reflect(siris, now)


def _goal_review(siris: "SirisAI", now: datetime) -> Any:
    from app.goals.review import review

    return review(siris, now)


def _backup(siris: "SirisAI", now: datetime) -> Any:
    from app.upkeep import backups

    return backups.make_backup(siris, now)


def _restore_drill(siris: "SirisAI", now: datetime) -> Any:
    from app.upkeep import backups

    return backups.restore_drill(siris, now)


def _housekeeping(siris: "SirisAI", now: datetime) -> Any:
    from app.upkeep import housekeeping

    return housekeeping.run(siris, now)


def _update_check(siris: "SirisAI", now: datetime) -> Any:
    from app.upkeep import updates

    return updates.check(siris, now)


def _model_trial(siris: "SirisAI", now: datetime) -> Any:
    from app.upkeep import models

    return models.weekly(siris, now)


JOBS: list[DailyJob] = [
    DailyJob("reflection", lambda s: s.settings.reflection_hour_local, _reflect),
    DailyJob("goal_review", lambda s: s.settings.goal_review_hour_local, _goal_review, weekday=lambda s: s.settings.goal_review_day),
    DailyJob("backup", lambda s: s.settings.backup_hour_local, _backup),
    DailyJob("restore_drill", lambda s: s.settings.restore_drill_hour_local, _restore_drill, monthday=1),
    DailyJob("housekeeping", lambda s: s.settings.housekeeping_hour_local, _housekeeping),
    DailyJob("update_check", lambda s: s.settings.update_check_hour_local if s.settings.update_mode != "off" else None, _update_check),
    DailyJob("model_trial", lambda s: s.settings.model_trial_hour_local if s.settings.candidate_models else None, _model_trial, weekday=lambda s: s.settings.model_trial_day),
]


def last_runs(siris: "SirisAI") -> dict[str, str]:
    return dict(siris.state.get(STATE_KEY) or {})


def due(siris: "SirisAI", job: DailyJob, local_now: datetime, runs: dict[str, str]) -> bool:
    hour = job.hour(siris)
    if hour is None or local_now.hour < hour or runs.get(job.name) == local_now.date().isoformat():
        return False
    weekday = job.weekday(siris)
    if weekday is not None and local_now.weekday() != weekday:
        return False
    return job.monthday is None or local_now.day == job.monthday


def run_now(siris: "SirisAI", job: DailyJob, now: datetime | None = None) -> Any:
    """Run one job now (POST /siris/learning/jobs/{name}/run). Doesn't count
    as its scheduled run, but its result is kept like one."""
    now = now or datetime.now(UTC)
    result = job.run(siris, now)
    kept = dict(siris.state.get(RESULTS_KEY) or {})
    kept[job.name] = {"at": now.isoformat(), "result": result}
    siris.state.set(RESULTS_KEY, kept)
    return result


def run_due(siris: "SirisAI", now: datetime | None = None, jobs: list[DailyJob] | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    local_now = now.astimezone(clock.get_zone(siris.settings.timezone))
    runs = last_runs(siris)
    results: dict[str, Any] = {}
    for job in jobs if jobs is not None else JOBS:
        if not due(siris, job, local_now, runs):
            continue
        try:
            results[job.name] = job.run(siris, now)
        except Exception as exc:  # noqa: BLE001 - one job failing never stops the others
            logger.exception("Daily job %s failed", job.name)
            results[job.name] = {"error": f"{type(exc).__name__}: {exc}"[:300]}
        runs[job.name] = local_now.date().isoformat()
        siris.state.set(STATE_KEY, runs)
    if results:
        kept = dict(siris.state.get(RESULTS_KEY) or {})
        for name, result in results.items():
            kept[name] = {"at": now.isoformat(), "result": result}
        siris.state.set(RESULTS_KEY, kept)
    return results
