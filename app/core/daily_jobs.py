"""Jobs that run once a day (or week, or month) at a set local hour: the
nightly reflection, the weekly goal review, and the upkeep jobs that keep
SirisAI running without anyone tending it (app/upkeep/: backups, the
restore drill, housekeeping, update checks, model trials), the morning
triage of email and calendar, and the evening CPD/evidence capture.

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


def _bills(siris: "SirisAI", now: datetime) -> Any:
    from app.bills import service

    return service.scan(siris, now=now)


def _travel(siris: "SirisAI", now: datetime) -> Any:
    from app.travel import service

    return [t["title"] for t in service.check(siris, now)]


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


def _triage(siris: "SirisAI", now: datetime) -> Any:
    from app.triage import morning

    return morning.run(siris, now)


def _career_capture(siris: "SirisAI", now: datetime) -> Any:
    from app.career import capture

    return capture.capture(siris, now)


def _server_updates(siris: "SirisAI", now: datetime) -> Any:
    from app.servers import watch

    return watch.check_updates(siris, now) if siris.ssh_client is not None else {"skipped": "no SSH servers"}


def _container_updates(siris: "SirisAI", now: datetime) -> Any:
    from app.homelab import containers

    return containers.check(siris, now)


def _server_backups(siris: "SirisAI", now: datetime) -> Any:
    from app.homelab import backups

    return backups.check(siris, now)


def _server_backup_test(siris: "SirisAI", now: datetime) -> Any:
    from app.homelab import backups

    return backups.restore_test(siris, now)


def _internet_weekly(siris: "SirisAI", now: datetime) -> Any:
    from app.homelab import internet

    return internet.weekly(siris, now)


def _expiry(siris: "SirisAI", now: datetime) -> Any:
    from app.homelab import expiry

    return expiry.check(siris, now)


def _energy_weekly(siris: "SirisAI", now: datetime) -> Any:
    from app.household import energy

    return energy.weekly(siris, now)


def _audit_weekly(siris: "SirisAI", now: datetime) -> Any:
    from app.audit import digest

    return digest.weekly(siris, now)


def _health_check(siris: "SirisAI", now: datetime) -> Any:
    from app.health import insights

    return {"getting_sick": insights.check_sick(siris, now)}


def _health_weekly(siris: "SirisAI", now: datetime) -> Any:
    from app.health import insights

    return insights.weekly(siris, now)


def _health_monthly(siris: "SirisAI", now: datetime) -> Any:
    from app.health import insights

    return insights.monthly(siris, now)


def _server_disks(siris: "SirisAI", now: datetime) -> Any:
    from app.servers import watch

    return watch.check_disks(siris, now) if siris.ssh_client is not None else {"skipped": "no SSH servers"}


JOBS: list[DailyJob] = [
    DailyJob("reflection", lambda s: s.settings.reflection_hour_local, _reflect),
    DailyJob("goal_review", lambda s: s.settings.goal_review_hour_local, _goal_review, weekday=lambda s: s.settings.goal_review_day),
    DailyJob("backup", lambda s: s.settings.backup_hour_local, _backup),
    DailyJob("restore_drill", lambda s: s.settings.restore_drill_hour_local, _restore_drill, monthday=1),
    DailyJob("housekeeping", lambda s: s.settings.housekeeping_hour_local, _housekeeping),
    DailyJob("update_check", lambda s: s.settings.update_check_hour_local if s.settings.update_mode != "off" else None, _update_check),
    DailyJob("morning_triage", lambda s: s.settings.triage_hour_local, _triage),
    DailyJob("career_capture", lambda s: s.settings.career_capture_hour_local, _career_capture),
    DailyJob("server_updates", lambda s: s.settings.server_updates_hour_local if s.ssh_client is not None else None, _server_updates),
    DailyJob("server_disks", lambda s: s.settings.server_disks_hour_local if s.ssh_client is not None else None, _server_disks),
    DailyJob("container_updates", lambda s: s.settings.container_updates_hour_local if getattr(s, "_docker_client", None) is not None else None, _container_updates),
    DailyJob("server_backups", lambda s: s.settings.server_backups_hour_local if s.settings.server_backups and s.ssh_client is not None else None, _server_backups),
    DailyJob("server_backup_test", lambda s: s.settings.server_backups_hour_local if s.settings.server_backups and s.ssh_client is not None else None, _server_backup_test, monthday=1),
    DailyJob("internet_weekly", lambda s: s.settings.internet_report_hour_local if s.settings.internet_probe_seconds > 0 else None, _internet_weekly, weekday=lambda s: 0),
    DailyJob("energy_weekly", lambda s: s.settings.energy_report_hour_local if getattr(s, "_home_assistant_client", None) is not None else None, _energy_weekly, weekday=lambda s: 0),
    DailyJob("audit_weekly", lambda s: s.settings.audit_digest_hour_local, _audit_weekly, weekday=lambda s: 6),
    DailyJob("bills", lambda s: s.settings.bills_hour_local if getattr(s, "email_client", None) is not None else None, _bills),
    DailyJob("travel", lambda s: s.settings.travel_hour_local if getattr(s, "calendar", None) is not None else None, _travel),
    DailyJob("health_check", lambda s: s.settings.health_check_hour_local, _health_check),
    DailyJob("health_weekly", lambda s: 19, _health_weekly, weekday=lambda s: 6),
    DailyJob("health_monthly", lambda s: 19, _health_monthly, monthday=1),
    DailyJob("expiry_check", lambda s: s.settings.expiry_check_hour_local if (s.settings.security_cert_hosts or s.settings.security_domains or (s.settings.expiry_check_servers and s.ssh_client is not None)) else None, _expiry),
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
