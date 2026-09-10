import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.siris import SirisAI, get_siris
from app.db.base import Base
from app.db.models import MetricSample
from app.metrics.service import MetricsStore
from app.notify.manager import NotificationManager
from app.skills.builtin.server_health import ServerHealthSkill


def _engine_and_metrics() -> tuple[sessionmaker, MetricsStore]:
    # An isolated in-memory DB per call, not the shared test-session DB --
    # ServerHealthSkill records to fixed metric names ("cpu_percent", ...),
    # so tests that seed history need their own store to avoid polluting
    # each other's baselines.
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine)
    return factory, MetricsStore(factory)


def _metrics() -> MetricsStore:
    return _engine_and_metrics()[1]


def _record_at_hour(factory: sessionmaker, metric_name: str, value: float, hour: int) -> None:
    """Inserts a MetricSample directly (bypassing MetricsStore.record_sample(),
    same reasoning test_goals.py's _backdate_goal() bypasses GoalStore) with
    created_at pinned to a specific hour-of-day, for hourly_baseline()
    tests that need to control which samples fall in which bucket."""
    with factory() as session:
        sample = MetricSample(metric_name=metric_name, value=value)
        session.add(sample)
        session.flush()
        sample.created_at = datetime.now(UTC).replace(hour=hour, tzinfo=None)
        session.commit()


def test_record_and_recent_values_most_recent_first() -> None:
    metrics = _metrics()
    for value in (10.0, 20.0, 30.0):
        metrics.record_sample("cpu_percent", value)

    assert metrics.recent_values("cpu_percent") == [30.0, 20.0, 10.0]


def test_recent_values_respects_limit() -> None:
    metrics = _metrics()
    for value in range(5):
        metrics.record_sample("cpu_percent", float(value))

    assert metrics.recent_values("cpu_percent", limit=2) == [4.0, 3.0]


def test_baseline_is_none_below_min_samples() -> None:
    metrics = _metrics()
    for value in (10.0, 10.0, 10.0):
        metrics.record_sample("cpu_percent", value)

    assert metrics.baseline("cpu_percent", min_samples=10) is None


def test_baseline_computes_mean_and_stdev_once_enough_samples() -> None:
    metrics = _metrics()
    values = [18.0, 20.0, 22.0, 19.0, 21.0, 20.0, 18.0, 22.0, 20.0, 19.0]
    for value in values:
        metrics.record_sample("cpu_percent", value)

    baseline = metrics.baseline("cpu_percent", min_samples=10)
    assert baseline is not None
    assert baseline.sample_count == 10
    assert 19.5 < baseline.mean < 20.5
    assert baseline.stdev > 0


# -- MetricsStore.hourly_baseline() (time-of-day anomaly detection)


def test_hourly_baseline_is_none_below_min_samples_for_that_hour() -> None:
    factory, metrics = _engine_and_metrics()
    for _ in range(4):
        _record_at_hour(factory, "cpu_percent", 2.0, hour=3)

    assert metrics.hourly_baseline("cpu_percent", hour=3, min_samples=5) is None


def test_hourly_baseline_only_considers_samples_from_that_hour() -> None:
    factory, metrics = _engine_and_metrics()
    # A busy daytime hour with a much higher mean...
    for _ in range(5):
        _record_at_hour(factory, "cpu_percent", 60.0, hour=14)
    # ...shouldn't leak into a quiet overnight hour's own baseline.
    for _ in range(5):
        _record_at_hour(factory, "cpu_percent", 2.0, hour=3)

    baseline = metrics.hourly_baseline("cpu_percent", hour=3, min_samples=5)
    assert baseline is not None
    assert baseline.sample_count == 5
    assert baseline.mean == 2.0


def test_hourly_baseline_computes_mean_and_stdev() -> None:
    factory, metrics = _engine_and_metrics()
    for value in (18.0, 20.0, 22.0, 19.0, 21.0):
        _record_at_hour(factory, "cpu_percent", value, hour=9)

    baseline = metrics.hourly_baseline("cpu_percent", hour=9, min_samples=5)
    assert baseline is not None
    assert baseline.sample_count == 5
    assert 19.5 < baseline.mean < 20.5
    assert baseline.stdev > 0


# -- ServerHealthSkill: personalized thresholds.


class _FakeToolsRegistry:
    def get(self, name: str) -> object | None:
        return None


class _FakeSettings:
    # Blank -- power-draw recording (milestone 47) is off for every test
    # in this file, same as it is by default.
    power_sensor_entity_id: str = ""


class _FakeSirisForServerHealth:
    """Its own isolated MetricsStore (so history genuinely accumulates and
    baseline() reflects only what this test seeded) but a canned
    system_info reading, so the "current value" side of the threshold
    check stays fully controlled by the test."""

    def __init__(self, cpu_percent: float) -> None:
        self.tools = _FakeToolsRegistry()
        self.metrics = _metrics()
        self.settings = _FakeSettings()
        self._cpu_percent = cpu_percent

    def run_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        assert name == "system_info"
        return {
            "cpu_percent": self._cpu_percent,
            "memory": {"percent_used": 10.0},
            "disk": {},
        }


def test_fresh_metric_with_no_history_uses_fixed_threshold_only() -> None:
    siris = _FakeSirisForServerHealth(cpu_percent=67.0)
    result = ServerHealthSkill().run(siris)
    assert result["healthy"] is True  # 67% is below the fixed 90% cutoff, no history yet to say otherwise

    siris_high = _FakeSirisForServerHealth(cpu_percent=95.0)
    result_high = ServerHealthSkill().run(siris_high)
    assert result_high["healthy"] is False
    assert result_high["warnings"] == ["CPU usage is high: 95.0%"]  # no baseline suffix -- none was used


def test_established_baseline_flags_a_value_the_fixed_threshold_would_miss() -> None:
    siris = _FakeSirisForServerHealth(cpu_percent=20.0)
    # 10 unflagged runs at a stable ~20% establish the baseline.
    for _ in range(10):
        ServerHealthSkill().run(siris)

    siris._cpu_percent = 40.0  # well above baseline, nowhere near the old fixed 90% threshold
    result = ServerHealthSkill().run(siris)

    assert result["healthy"] is False
    assert len(result["warnings"]) == 1
    assert result["warnings"][0].startswith("CPU usage is high: 40.0%")
    assert "baseline" in result["warnings"][0]


def test_hourly_baseline_is_preferred_and_labeled_when_available() -> None:
    """A value unremarkable against a flat day/night average can still
    be genuinely unusual for one specific hour -- the exact gap
    hourly_baseline() exists to close (see its own module docstring)."""
    factory, metrics = _engine_and_metrics()
    current_hour = datetime.now(UTC).hour
    for _ in range(5):
        _record_at_hour(factory, "cpu_percent", 2.0, hour=current_hour)

    siris = _FakeSirisForServerHealth(cpu_percent=40.0)
    siris.metrics = metrics

    result = ServerHealthSkill().run(siris)

    assert result["healthy"] is False
    assert result["warnings"][0].startswith("CPU usage is high: 40.0%")
    assert "hourly baseline" in result["warnings"][0]


def test_personalized_threshold_never_exceeds_the_fixed_ceiling() -> None:
    siris = _FakeSirisForServerHealth(cpu_percent=85.0)
    # A server that normally runs hot: baseline mean+2*stdev would exceed
    # 90 here, but the fixed threshold is still a hard ceiling.
    for value in (80.0, 82.0, 84.0, 86.0, 88.0, 90.0, 80.0, 82.0, 84.0, 86.0):
        siris.metrics.record_sample("cpu_percent", value)

    siris._cpu_percent = 88.0
    assert ServerHealthSkill().run(siris)["healthy"] is True  # below the 90% ceiling, still fine

    siris._cpu_percent = 92.0
    result = ServerHealthSkill().run(siris)
    assert result["healthy"] is False
    assert result["warnings"][0].startswith("CPU usage is high: 92.0%")


def test_baseline_triggered_disk_warning_still_correlates_via_notification_manager() -> None:
    """A personalized-threshold warning keeps the exact "X is high: Y%"
    prefix/shape milestone 26's regex-based correlation depends on."""
    container = f"plex-{uuid.uuid4()}"
    disk = f"media-storage-{uuid.uuid4()}"

    class _DiskSiris:
        def __init__(self, disk_percent: float) -> None:
            self.tools = _FakeToolsRegistry()
            self.metrics = _metrics()
            self.settings = _FakeSettings()
            self._disk_percent = disk_percent

        def run_tool(self, name: str, arguments: dict[str, Any]) -> Any:
            return {
                "cpu_percent": 5.0,
                "memory": {"percent_used": 5.0},
                "disk": {disk: {"percent_used": self._disk_percent}},
            }

    disk_siris = _DiskSiris(disk_percent=20.0)
    for _ in range(10):
        ServerHealthSkill().run(disk_siris)
    disk_siris._disk_percent = 40.0
    result = ServerHealthSkill().run(disk_siris)
    disk_warning = result["warnings"][0]
    assert "baseline" in disk_warning

    full_siris = SirisAI(settings=get_siris().settings)
    full_siris.entities.add_relationship(container, "DEPENDS_ON", disk)
    manager = NotificationManager(full_siris.entities)

    items = manager.summarize([f"Container '{container}' is exited", disk_warning])
    assert len(items) == 1
    assert items[0].category == "correlated"
