"""app/notify/proactive.py (the checks POST /siris/proactive/* wraps) and
their new second caller, the background scheduler
(app/core/scheduler.py) -- running the same checks on a schedule instead
of needing an external cron job. tests/test_explainable_autonomy.py and
tests/test_daily_briefing.py already cover the checks' own behavior in
detail; these focus on what's new: the scheduler actually calling them,
on the right cadence, tolerating failures, and the daily briefing firing
exactly once per calendar day."""

from datetime import UTC, datetime, timedelta

import pytest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core import scheduler
from app.core.config import Settings
from app.core.scheduler_state import SchedulerStateStore
from app.core.siris import SirisAI
from app.db.base import Base


def _isolated_scheduler_state() -> SchedulerStateStore:
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    return SchedulerStateStore(sessionmaker(bind=engine))


def _siris(**settings: object) -> SirisAI:
    """A real SirisAI, but with its own isolated scheduler_state --
    SchedulerState is a genuine single global row (see its own docstring),
    so sharing the real SessionLocal across tests would let one test's
    mark_briefing_sent() leak into another's "has it already run today"
    check."""
    siris = SirisAI(settings=Settings(timezone="Australia/Melbourne", **settings))  # type: ignore[arg-type]
    siris.scheduler_state = _isolated_scheduler_state()
    return siris


# -- Settings.briefing_hour_local: a blank .env line means unset, same as
# weather_latitude/longitude already handle (an unset .env line arrives as
# "" rather than being omitted, which int | None otherwise rejects).


def test_a_blank_briefing_hour_env_value_means_disabled() -> None:
    assert Settings(briefing_hour_local="").briefing_hour_local is None  # type: ignore[arg-type]


def test_an_actual_hour_is_kept() -> None:
    assert Settings(briefing_hour_local=9).briefing_hour_local == 9


# -- SchedulerStateStore


def test_scheduler_state_round_trips_the_last_briefing_date() -> None:
    store = _isolated_scheduler_state()
    assert store.last_briefing_date() is None  # no row yet
    store.mark_briefing_sent("2000-01-01")
    assert store.last_briefing_date() == "2000-01-01"
    store.mark_briefing_sent("2000-01-02")
    assert store.last_briefing_date() == "2000-01-02"  # updates in place, not a second row


# -- maybe_run_daily_briefing


def test_briefing_disabled_when_hour_is_unset() -> None:
    siris = _siris(briefing_hour_local=None)
    assert scheduler.maybe_run_daily_briefing(siris, datetime(2026, 9, 28, 23, 0, tzinfo=UTC)) is False


def test_briefing_does_not_fire_before_the_configured_local_hour() -> None:
    siris = _siris(briefing_hour_local=7)
    # 2026-09-28 20:00 UTC is 2026-09-29 06:00 in Melbourne (AEST) -- before 7am local.
    assert scheduler.maybe_run_daily_briefing(siris, datetime(2026, 9, 28, 20, 0, tzinfo=UTC)) is False


def test_briefing_fires_once_at_or_past_the_configured_local_hour() -> None:
    siris = _siris(briefing_hour_local=7)
    # 2026-09-28 21:30 UTC is 2026-09-29 07:30 in Melbourne -- past 7am local.
    at_hour = datetime(2026, 9, 28, 21, 30, tzinfo=UTC)

    assert scheduler.maybe_run_daily_briefing(siris, at_hour) is True
    assert siris.scheduler_state.last_briefing_date() == "2026-09-29"
    # Same local calendar day, later tick -- does not fire again.
    assert scheduler.maybe_run_daily_briefing(siris, at_hour + timedelta(hours=2)) is False


def test_briefing_fires_again_the_next_calendar_day() -> None:
    siris = _siris(briefing_hour_local=7)
    day_one = datetime(2026, 9, 28, 21, 30, tzinfo=UTC)
    day_two = day_one + timedelta(days=1)

    assert scheduler.maybe_run_daily_briefing(siris, day_one) is True
    assert scheduler.maybe_run_daily_briefing(siris, day_two) is True
    assert siris.scheduler_state.last_briefing_date() == "2026-09-30"


def test_briefing_actually_sends_a_notification(monkeypatch: pytest.MonkeyPatch) -> None:
    siris = _siris(briefing_hour_local=7)
    notified = []
    monkeypatch.setattr(siris, "run_tool", lambda name, args, **kw: (notified.append(name), {"summary": "all quiet"})[1])

    scheduler.maybe_run_daily_briefing(siris, datetime(2026, 9, 28, 21, 30, tzinfo=UTC))

    assert "daily_briefing" in notified


def test_a_failing_briefing_is_still_marked_sent_not_retried_all_day(monkeypatch: pytest.MonkeyPatch) -> None:
    """A broken briefing should surface once (in the logs) and get fixed,
    not hammer the same failure every scheduler tick for the rest of the day."""
    siris = _siris(briefing_hour_local=7)

    def boom(name: str, args: dict, **kw: object) -> dict:
        raise RuntimeError("daily_briefing exploded")

    monkeypatch.setattr(siris, "run_tool", boom)
    at_hour = datetime(2026, 9, 28, 21, 30, tzinfo=UTC)

    assert scheduler.maybe_run_daily_briefing(siris, at_hour) is True  # "ran" (attempted), didn't raise
    assert siris.scheduler_state.last_briefing_date() == "2026-09-29"
    assert scheduler.maybe_run_daily_briefing(siris, at_hour + timedelta(minutes=5)) is False  # not retried


# -- run_proactive_check / run_presence_and_geofence_checks: best-effort


def test_run_proactive_check_does_not_raise_when_a_composed_tool_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    siris = _siris()

    def boom(name: str, args: dict, **kw: object) -> dict:
        raise RuntimeError("event_timing_check exploded")

    monkeypatch.setattr(siris, "run_tool", boom)
    scheduler.run_proactive_check(siris)  # must not raise


def test_run_presence_and_geofence_checks_attempts_both_even_if_the_first_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    def fake_presence(s: SirisAI, trigger: str) -> None:
        calls.append("presence")
        raise RuntimeError("presence backend down")

    def fake_geofence(s: SirisAI, trigger: str) -> dict:
        calls.append("geofence")
        return {"fired": []}

    monkeypatch.setattr("app.core.scheduler.proactive.run_presence_check", fake_presence)
    monkeypatch.setattr("app.core.scheduler.proactive.run_geofence_check", fake_geofence)
    monkeypatch.setattr(scheduler, "_has_geofence_reminders", lambda s: True)

    scheduler.run_presence_and_geofence_checks(_siris())  # must not raise

    assert calls == ["presence", "geofence"]


def test_the_geofence_check_is_skipped_with_no_reminders(monkeypatch: pytest.MonkeyPatch) -> None:
    """With nothing to watch it isn't run -- and so isn't logged -- every few minutes."""
    calls = []
    monkeypatch.setattr("app.core.scheduler.proactive.run_presence_check", lambda s, trigger: calls.append("presence"))
    monkeypatch.setattr("app.core.scheduler.proactive.run_geofence_check", lambda s, trigger: calls.append("geofence"))
    siris = _siris()
    monkeypatch.setattr(siris.geofence, "list_all", lambda enabled_only=False, **k: [])
    scheduler.run_presence_and_geofence_checks(siris)
    assert calls == ["presence"]
    monkeypatch.setattr(siris.geofence, "list_all", lambda enabled_only=False, **k: [{"id": 1}])
    scheduler.run_presence_and_geofence_checks(siris)
    assert calls == ["presence", "presence", "geofence"]


def test_presence_and_geofence_are_genuine_no_ops_when_not_configured() -> None:
    """Default Settings() has no UniFi/Home Assistant configured -- both
    checks should quietly do nothing, not error."""
    siris = _siris()
    scheduler.run_presence_and_geofence_checks(siris)  # must not raise


# -- tick() wiring


def test_tick_reports_whether_the_briefing_ran() -> None:
    siris = _siris(briefing_hour_local=None)  # disabled, so deterministically False
    result = scheduler.tick(siris, datetime.now(UTC), run_health=False)
    assert result["briefing_sent"] is False
    assert "reminders_sent" in result


# -- run(): the interval math actually gates each job


def test_run_calls_each_job_on_its_own_cadence(monkeypatch: pytest.MonkeyPatch) -> None:
    """A short-lived run() loop (cancelled after a couple of ticks) with
    tiny intervals should call the proactive/presence jobs, not just
    reminders/health -- proving they're actually wired into the loop, not
    just independently correct functions nothing calls."""
    import asyncio

    siris = _siris(proactive_check_interval_minutes=0.001, presence_check_interval_minutes=0.001)
    calls: list[str] = []
    monkeypatch.setattr(scheduler, "run_proactive_check", lambda s, now=None: calls.append("proactive"))
    monkeypatch.setattr(scheduler, "run_presence_and_geofence_checks", lambda s: calls.append("presence"))
    monkeypatch.setattr(scheduler, "deliver_due_reminders", lambda s, now=None: 0)
    monkeypatch.setattr(scheduler, "maybe_run_daily_briefing", lambda s, now=None: False)

    async def go() -> None:
        task = asyncio.create_task(scheduler.run(siris, interval_seconds=0.01))
        # Wait for the presence job (due on every loop) rather than a fixed
        # 50 ms: on a loaded CI box the first loop can take longer than that.
        for _ in range(200):
            await asyncio.sleep(0.01)
            if "presence" in calls:
                break
        await asyncio.sleep(0.03)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(go())

    assert "proactive" in calls
    assert "presence" in calls


def test_run_skips_a_job_whose_interval_is_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    siris = _siris(proactive_check_interval_minutes=0, presence_check_interval_minutes=0.001)
    calls: list[str] = []
    monkeypatch.setattr(scheduler, "run_proactive_check", lambda s, now=None: calls.append("proactive"))
    monkeypatch.setattr(scheduler, "run_presence_and_geofence_checks", lambda s: calls.append("presence"))
    monkeypatch.setattr(scheduler, "deliver_due_reminders", lambda s, now=None: 0)
    monkeypatch.setattr(scheduler, "maybe_run_daily_briefing", lambda s, now=None: False)

    async def go() -> None:
        task = asyncio.create_task(scheduler.run(siris, interval_seconds=0.01))
        # Wait for the presence job (due on every loop) rather than a fixed
        # 50 ms: on a loaded CI box the first loop can take longer than that.
        for _ in range(200):
            await asyncio.sleep(0.01)
            if "presence" in calls:
                break
        await asyncio.sleep(0.03)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(go())

    assert "proactive" not in calls
    assert "presence" in calls
