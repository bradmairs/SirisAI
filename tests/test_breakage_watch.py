"""5.8 breakage watch: pushed within the hour, with the fix; not for blips; again daily; and when fixed."""

from datetime import UTC, datetime, timedelta

import pytest

from app.core import scheduler
from app.core.config import Settings
from app.core.siris import SirisAI
from app.db.models import ToolExecution
from app.upkeep import breakage, reliability

NOW = datetime(2030, 1, 15, 10, 0, tzinfo=UTC)   # clear of any real rows in the shared test database


class FakeNotify:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send(self, message: str, title: str | None = None, actions=None, priority=None) -> None:
        self.sent.append((title or "", message))

    def close(self) -> None:
        pass


@pytest.fixture
def siris(monkeypatch: pytest.MonkeyPatch):
    s = SirisAI(settings=Settings(ntfy_topic="t"))
    s._notify_client = FakeNotify()
    s.tools.get("notify")._client = s._notify_client
    s.state.set(breakage.STATE_KEY, {})
    s.state.set(reliability.FAILURES_KEY, [])
    monkeypatch.setattr(scheduler, "JOB_STATUS", {})
    monkeypatch.setattr("app.core.health.broken_tools", lambda siris, now=None: set())
    yield s
    with s.audit._session_factory() as session:   # remove only the rows these tests added
        session.query(ToolExecution).filter(ToolExecution.created_at >= datetime(2030, 1, 1)).delete()
        session.commit()


def _call(s: SirisAI, tool: str, minutes_ago: float, error: str | None) -> None:
    with s.audit._session_factory() as session:
        session.add(ToolExecution(tool_name=tool, arguments="{}", error=error, duration_ms=1,
                                  created_at=(NOW - timedelta(minutes=minutes_ago)).replace(tzinfo=None)))
        session.commit()


def test_an_expired_token_is_pushed_once_with_the_fix(siris: SirisAI) -> None:
    for m in (50, 30, 5):
        _call(siris, "home_assistant_get_state", m, "Client error '401 Unauthorized' for url 'http://ha/api/states'")
    said = breakage.check(siris, NOW)
    assert said["started"] == ["tool:home_assistant_get_state"]
    title, body = siris._notify_client.sent[-1]
    assert "Home Assistant keeps failing" in title and "expired or been replaced" in body
    assert breakage.check(siris, NOW + timedelta(minutes=15))["started"] == []      # not again
    assert len(siris._notify_client.sent) == 1


def test_a_short_blip_isnt_a_breakage(siris: SirisAI) -> None:
    for m in (9, 6, 3):   # the gateway's nightly restart: a few minutes of refusals
        _call(siris, "unifi_list_clients", m, "[Errno 111] Connection refused")
    assert breakage.find(siris, NOW) == {}


def test_a_success_after_the_failures_means_its_fine(siris: SirisAI) -> None:
    for m in (50, 30, 10):
        _call(siris, "docker_list_containers", m, "timed out")
    _call(siris, "docker_list_containers", 2, None)
    assert breakage.find(siris, NOW) == {}


def test_user_mistakes_dont_count(siris: SirisAI) -> None:
    for m in (50, 30, 10):
        _call(siris, "home_assistant_get_state", m, "No entity called 'lounge lamp'")
    assert breakage.find(siris, NOW) == {}


def test_a_failing_background_job_is_caught(siris: SirisAI) -> None:
    scheduler.JOB_STATUS["house_watch"] = {"last_error": "HTTPStatusError(\"Client error '401 Unauthorized'\")"}
    siris.state.set(reliability.FAILURES_KEY, [
        {"at": (NOW - timedelta(minutes=m)).isoformat(), "job": "house_watch", "error": "401", "slow": False} for m in (40, 10)])
    found = breakage.find(siris, NOW)
    assert found["job:house_watch"]["title"] == "The house watch job keeps failing"
    assert "Home Assistant is refusing" in found["job:house_watch"]["body"]


def test_ongoing_breakage_is_repeated_daily_and_its_fix_announced(siris: SirisAI) -> None:
    for m in (50, 30, 5):
        _call(siris, "plex_search", m, "Server error '502 Bad Gateway'")
    breakage.check(siris, NOW)
    later = NOW + timedelta(days=1, minutes=1)
    for m in (50, 30, 5):
        with siris.audit._session_factory() as session:
            session.add(ToolExecution(tool_name="plex_search", arguments="{}", error="Server error '502 Bad Gateway'",
                                      duration_ms=1, created_at=(later - timedelta(minutes=m)).replace(tzinfo=None)))
            session.commit()
    assert breakage.check(siris, later)["ongoing"] == ["tool:plex_search"]
    assert "Still broken (1 day)" in siris._notify_client.sent[-1][1]

    nothing = {"started": [], "ongoing": [], "fixed": [], "dropped": []}
    assert breakage.check(siris, later + timedelta(hours=2)) == nothing   # quiet, but not proved fixed
    _call(siris, "plex_search", -(60 * 24 + 1 + 120), None)              # then someone uses Plex and it works
    assert breakage.check(siris, later + timedelta(hours=2, minutes=15))["fixed"] == ["tool:plex_search"]
    assert siris._notify_client.sent[-1] == ("SirisAI: fixed", "Working again: Plex.")
    assert all(not i["title"].startswith("Plex keeps") for i in siris.attention.list())


def test_going_quiet_isnt_announced_as_fixed_but_clears_after_a_day(siris: SirisAI) -> None:
    for m in (50, 30, 5):
        _call(siris, "home_assistant_get_state", m, "Client error '401 Unauthorized'")
    breakage.check(siris, NOW)
    assert breakage.check(siris, NOW + timedelta(hours=3))["fixed"] == []   # nobody asked since: unknown
    assert breakage.check(siris, NOW + timedelta(days=1, minutes=5))["dropped"] == ["tool:home_assistant_get_state"]
    assert len(siris._notify_client.sent) == 1   # no "fixed" push


def test_a_job_is_fixed_when_its_next_run_passes(siris: SirisAI) -> None:
    scheduler.JOB_STATUS["house_watch"] = {"runs": 5, "last_error": "HTTPStatusError('401 Unauthorized')"}
    siris.state.set(reliability.FAILURES_KEY, [
        {"at": (NOW - timedelta(minutes=m)).isoformat(), "job": "house_watch", "error": "401", "slow": False} for m in (40, 10)])
    breakage.check(siris, NOW)
    scheduler.JOB_STATUS["house_watch"] = {"runs": 6, "last_error": None}
    assert breakage.check(siris, NOW + timedelta(minutes=15))["fixed"] == ["job:house_watch"]
