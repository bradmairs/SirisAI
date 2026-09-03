import uuid
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.siris import SirisAI, get_siris
from app.db.base import Base
from app.memory.events import EventMemoryStore
from app.notify.manager import NotificationManager
from app.skills.builtin.event_timing import EventTimingSkill


def _siris() -> SirisAI:
    return SirisAI(settings=get_siris().settings)


class _FakeSirisWithIsolatedEvents:
    """Just enough of SirisAI's surface for EventTimingSkill.run() -- an
    isolated in-memory events store, not the shared test-session DB.
    distinct_recurring_signatures() scans globally by design (any
    signature that's recurred enough, whichever test created it), so a
    test asserting "nothing is flagged" needs a store no other test has
    ever written to -- same isolation test_metrics.py uses for
    MetricsStore, for the same reason."""

    def __init__(self) -> None:
        engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(bind=engine)
        self.events = EventMemoryStore(sessionmaker(bind=engine))


def _seed_regular_events(
    siris: SirisAI, description: str, entity_name: str | None, count: int, interval_hours: float, now: datetime
) -> None:
    """Logs `count` occurrences of the same event signature, spaced
    exactly `interval_hours` apart, most recent at `now - interval_hours`
    (i.e. one interval overdue relative to `now` by design -- tests pass
    their own `now` to EventTimingSkill.run() to control what "overdue"
    means without depending on wall-clock time)."""
    for i in range(count, 0, -1):
        siris.events.log_event(
            description, entity_name=entity_name, created_at=now - timedelta(hours=interval_hours * i)
        )


# -- EventMemoryStore.distinct_recurring_signatures / timing_baseline


def test_distinct_recurring_signatures_excludes_one_off_events() -> None:
    siris = _siris()
    description = f"one-off-{uuid.uuid4()}"
    siris.events.log_event(description)

    signatures = siris.events.distinct_recurring_signatures(min_samples=5)
    assert (description, None) not in signatures


def test_distinct_recurring_signatures_includes_events_at_the_sample_threshold() -> None:
    siris = _siris()
    description = f"recurring-{uuid.uuid4()}"
    for _ in range(5):
        siris.events.log_event(description)

    signatures = siris.events.distinct_recurring_signatures(min_samples=5)
    assert (description, None) in signatures


def test_distinct_recurring_signatures_scopes_by_entity_name() -> None:
    siris = _siris()
    description = f"backup completed-{uuid.uuid4()}"
    entity = f"nas-{uuid.uuid4()}"
    for _ in range(5):
        siris.events.log_event(description, entity_name=entity)
    # Only 2 occurrences with no entity -- shouldn't meet the threshold.
    for _ in range(2):
        siris.events.log_event(description)

    signatures = siris.events.distinct_recurring_signatures(min_samples=5)
    assert (description, entity) in signatures
    assert (description, None) not in signatures


def test_timing_baseline_is_none_below_min_samples() -> None:
    siris = _siris()
    description = f"sparse-{uuid.uuid4()}"
    siris.events.log_event(description)
    siris.events.log_event(description)

    assert siris.events.timing_baseline(description, min_samples=5) is None


def test_timing_baseline_computes_mean_and_stdev_of_intervals() -> None:
    siris = _siris()
    description = f"nightly-backup-{uuid.uuid4()}"
    now = datetime(2026, 1, 10, tzinfo=UTC).replace(tzinfo=None)
    _seed_regular_events(siris, description, None, count=6, interval_hours=24.0, now=now)

    baseline = siris.events.timing_baseline(description, min_samples=5)
    assert baseline is not None
    assert baseline.sample_count == 6
    assert abs(baseline.mean_interval_hours - 24.0) < 0.01
    assert baseline.stdev_interval_hours < 0.01  # perfectly regular in this fixture
    assert baseline.last_occurred_at == now - timedelta(hours=24.0)


# -- EventTimingSkill


def test_event_timing_skill_flags_an_overdue_recurring_event() -> None:
    siris = _siris()
    description = f"nightly-backup-{uuid.uuid4()}"
    now = datetime(2026, 1, 10, tzinfo=UTC).replace(tzinfo=None)
    # Regular every 24h, but the "current" check time is 3 days past the
    # last occurrence -- well past mean + 2*stdev (stdev ~0 here).
    _seed_regular_events(siris, description, None, count=6, interval_hours=24.0, now=now)
    check_time = now + timedelta(days=2)

    result = EventTimingSkill().run(siris, now=check_time)

    assert result["healthy"] is False
    assert any(description in w for w in result["warnings"])
    assert any(e["description"] == description for e in result["overdue_events"])


def test_event_timing_skill_does_not_flag_an_on_time_event() -> None:
    siris = _siris()
    description = f"nightly-backup-{uuid.uuid4()}"
    now = datetime(2026, 1, 10, tzinfo=UTC).replace(tzinfo=None)
    _seed_regular_events(siris, description, None, count=6, interval_hours=24.0, now=now)
    # Checking right at the last occurrence -- nowhere near overdue.
    check_time = now - timedelta(hours=24.0)

    result = EventTimingSkill().run(siris, now=check_time)

    assert not any(description in w for w in result["warnings"])


def test_event_timing_skill_healthy_with_no_recurring_history() -> None:
    siris = _FakeSirisWithIsolatedEvents()
    result = EventTimingSkill().run(siris, now=datetime.now(UTC).replace(tzinfo=None))
    assert result["healthy"] is True
    assert result["overdue_events"] == []


def test_event_timing_skill_ignores_a_burst_of_rapid_fire_events() -> None:
    """Caught live: several events logged back to back (e.g. retries, or a
    webhook firing repeatedly) have a near-zero mean interval, which makes
    *any* later gap -- even milliseconds -- look wildly "overdue" by the
    raw mean+2*stdev math. A signature whose own history clusters within
    less than an hour never even becomes a candidate."""
    siris = _FakeSirisWithIsolatedEvents()
    description = f"burst-event-{uuid.uuid4()}"
    now = datetime(2026, 1, 10, tzinfo=UTC).replace(tzinfo=None)
    # 5 occurrences a few seconds apart -- a burst, not a schedule.
    for i in range(5, 0, -1):
        siris.events.log_event(description, created_at=now - timedelta(seconds=i))

    result = EventTimingSkill().run(siris, now=now + timedelta(hours=6))

    assert not any(description in w for w in result["warnings"])
    assert result["healthy"] is True


# -- NotificationManager classification


def test_notification_manager_classifies_overdue_event_as_timing_category() -> None:
    siris = _siris()
    items = NotificationManager(siris.entities).summarize(
        ["Expected recurring event is overdue: 'nightly-backup' -- last seen 50.0h ago, usually every 24.0h ± 0.5h (10 occurrences)"]
    )
    assert items[0].category == "timing"
    assert items[0].action == "notify"
    assert items[0].severity == "warning"


# -- Endpoint-level: POST /siris/proactive/check composes event_timing_check


def test_proactive_check_includes_event_timing_result(client: TestClient) -> None:
    response = client.post("/siris/proactive/check")
    assert response.status_code == 200
    body = response.json()
    assert "event_timing" in body
    assert "warnings" in body["event_timing"]
    assert "healthy" in body["event_timing"]


def test_event_timing_check_tool_is_registered_as_a_skill() -> None:
    siris = _siris()
    assert siris.tools.get("event_timing_check") is not None
    assert "event_timing_check" in siris.capabilities()["skills"]
