import asyncio
from datetime import UTC, date, datetime
from typing import Any

import pytest

from app.core.config import Settings
from app.core.siris import SirisAI
from app.llm.base import LLMResponse
from app.plugins.base import Tool, ToolPermission
from app.second_brain import jobs

pytestmark = pytest.mark.usefixtures("client")  # creates the shared test database's tables

DAY = date(2026, 10, 6)  # a Tuesday


class FakeBrain:
    def __init__(self, notes: dict[str, str] | None = None, gaps: list[dict] | None = None) -> None:
        self.notes = notes or {}
        self._gaps = gaps or []
        self.learned: list[dict] = []

    def learn(self, items: list[dict]) -> list[dict]:
        self.learned += items
        return [{"action": "learned", "title": i["title"], "path": f"Notes/{i['title']}.md", "links": []} for i in items]

    def get_note(self, title: str) -> dict | None:
        return {"title": title, "body": self.notes[title], "path": ""} if title in self.notes else None

    def gaps(self) -> list[dict]:
        return self._gaps

    def list_notes(self) -> list[dict]:
        return [{"title": t, "type": "note", "aliases": [], "path": ""} for t in self.notes]


class FakeCalendar(Tool):
    name = "calendar_upcoming_events"
    description = "fake"
    permission = ToolPermission.READ
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, events: list[dict]) -> None:
        self.events = events

    def run(self, **kwargs: Any) -> Any:
        return self.events


class FakeLLM:
    def __init__(self, text: str) -> None:
        self.text, self.messages = text, None

    async def generate(self, messages: list[dict], **kwargs: Any) -> LLMResponse:
        self.messages = messages
        return LLMResponse(text=self.text)


class FakeNotify:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send(self, message: str, title: str = "", **kwargs: Any) -> None:
        self.sent.append((title, message))

    def close(self) -> None:
        pass


def _siris(**settings: Any) -> SirisAI:
    siris = SirisAI(settings=Settings(timezone="Australia/Melbourne", **settings))
    siris.state.set(jobs.STATE_KEY, {})
    siris.state.set(jobs.ASKED_KEY, {})
    return siris


def _at(hour: int, day: date = DAY) -> datetime:
    """A UTC instant that is `hour`:00 on `day` in Melbourne (UTC+11 in October)."""
    local = datetime(day.year, day.month, day.day, hour, tzinfo=jobs.clock.get_zone("Australia/Melbourne"))
    return local.astimezone(UTC)


# ---------- scheduling ----------


def test_nothing_runs_without_a_brain() -> None:
    siris = _siris()
    assert asyncio.run(jobs.run_due(siris, _at(23))) == {}


def test_meetings_run_once_a_day_from_their_hour() -> None:
    siris = _siris(brain_dream_hour_local=None, brain_weekly_hour_local=None)
    siris.second_brain = FakeBrain()
    siris.tools.register(FakeCalendar([]))
    assert asyncio.run(jobs.run_due(siris, _at(5))) == {}          # before 06:00
    assert "meetings" in asyncio.run(jobs.run_due(siris, _at(6)))
    assert asyncio.run(jobs.run_due(siris, _at(9))) == {}          # already ran today
    assert "meetings" in asyncio.run(jobs.run_due(siris, _at(6, date(2026, 10, 7))))


def test_weekly_only_on_its_day() -> None:
    siris = _siris(brain_meeting_notes_hour_local=None, brain_dream_hour_local=None, brain_weekly_day=1)
    siris.second_brain = FakeBrain()
    assert "weekly" in asyncio.run(jobs.run_due(siris, _at(18)))   # Tuesday = 1
    siris.state.set(jobs.STATE_KEY, {})
    assert asyncio.run(jobs.run_due(siris, _at(18, date(2026, 10, 7)))) == {}


def test_a_failing_job_is_reported_and_not_retried_today() -> None:
    siris = _siris(brain_dream_hour_local=None, brain_weekly_hour_local=None)
    siris.second_brain = FakeBrain()

    class Broken(FakeCalendar):
        def run(self, **kwargs: Any) -> Any:
            raise RuntimeError("calendar down")

    siris.tools.register(Broken([]))
    assert "calendar down" in asyncio.run(jobs.run_due(siris, _at(7)))["meetings"]["error"]
    assert asyncio.run(jobs.run_due(siris, _at(8))) == {}


# ---------- meetings ----------


def test_meeting_notes_for_todays_timed_events() -> None:
    siris = _siris(brain_meeting_calendars="Work")
    brain = siris.second_brain = FakeBrain({"Already Noted (2026-10-06)": "x"})
    siris.tools.register(FakeCalendar([
        {"summary": "GVW design review with Aaron Cheong", "start": "2026-10-06T10:00:00+11:00",
         "end": "2026-10-06T11:00:00+11:00", "location": "Shepparton", "calendar": "Work"},
        {"summary": "Public holiday", "start": "2026-10-06", "calendar": "Work"},             # all-day
        {"summary": "Tomorrow thing", "start": "2026-10-07T09:00:00+11:00", "calendar": "Work"},
        {"summary": "Dentist", "start": "2026-10-06T15:00:00+11:00", "calendar": "Personal"},  # filtered out
        {"summary": "Already Noted", "start": "2026-10-06T16:00:00+11:00", "calendar": "Work"},
    ]))
    assert jobs.write_meeting_notes(siris, DAY) == {"created": ["GVW design review with Aaron Cheong (2026-10-06)"]}
    content = brain.learned[0]["content"]
    assert "**When:** 2026-10-06 10:00–11:00" in content and "**Where:** Shepparton" in content
    assert "## Action items" in content and brain.learned[0]["tags"] == ["meeting"]


def test_meeting_notes_need_a_calendar() -> None:
    siris = _siris()
    siris.second_brain = FakeBrain()
    assert jobs.write_meeting_notes(siris, DAY) == {"skipped": "no calendar configured"}


# ---------- dream ----------


def test_parse_notes_is_strict() -> None:
    text = 'Sure!\n```json\n{"notes": [{"title": "Home Server", "content": "Has 32 GB RAM.", "tags": ["Hardware"]},' \
           ' {"title": "", "content": "no title"}, {"title": "X", "content": "y", "type": "spaceship"}, "junk"]}\n```'
    assert jobs.parse_notes(text) == [
        {"title": "Home Server", "content": "Has 32 GB RAM.", "type": "note", "tags": ["hardware"]},
        {"title": "X", "content": "y", "type": "note", "tags": []},
    ]
    assert jobs.parse_notes("not json") == []
    assert jobs.parse_notes(None) == []
    many = '{"notes": [' + ",".join('{"title": "T%d", "content": "c"}' % i for i in range(20)) + "]}"
    assert len(jobs.parse_notes(many)) == 10


def test_day_material_includes_todays_memory_inbox_entries() -> None:
    siris = _siris()
    siris.second_brain = FakeBrain({jobs.MEMORY_INBOX_TITLE: "## Updates\n\n### 2026-10-05\nold fact\n\n### 2026-10-06\nBrad prefers flat whites.\n"})
    text = jobs.day_material(siris, DAY)
    assert "Brad prefers flat whites." in text and "old fact" not in text


def test_dream_turns_the_day_into_tagged_notes(monkeypatch: pytest.MonkeyPatch) -> None:
    siris = _siris()
    brain = siris.second_brain = FakeBrain({"Home Server": "i5"})
    monkeypatch.setattr(jobs, "day_material", lambda s, d: "Brad: I upgraded the server to 32 GB RAM.")
    siris.llm = FakeLLM('{"notes": [{"title": "Home Server", "content": "Upgraded to 32 GB RAM."}]}')
    result = asyncio.run(jobs.dream(siris, DAY))
    assert result == {"learned": ["Home Server"]}
    assert brain.learned[0]["tags"] == ["siris-dream"]
    assert brain.learned[0]["source"] == "SirisAI conversations, 2026-10-06"
    assert "Existing note titles: Home Server" in siris.llm.messages[1]["content"]


def test_dream_skips_a_quiet_day(monkeypatch: pytest.MonkeyPatch) -> None:
    siris = _siris()
    siris.second_brain = FakeBrain()
    monkeypatch.setattr(jobs, "day_material", lambda s, d: "")
    assert asyncio.run(jobs.dream(siris, DAY)) == {"skipped": "nothing new today"}


# ---------- weekly ----------


def test_gap_questions_rotate_through_unasked_gaps_first() -> None:
    siris = _siris(brain_gap_questions=2)
    gaps = [{"title": f"Note {i}", "kind": "fill_in", "detail": f"detail {i}"} for i in range(3)]
    siris.second_brain = FakeBrain(gaps=gaps)
    siris._notify_client = FakeNotify()
    assert jobs.ask_gap_questions(siris, DAY) == {"asked": ["Note 0", "Note 1"]}
    assert "1. Note 0: detail 0" in siris._notify_client.sent[0][1]
    assert jobs.ask_gap_questions(siris, date(2026, 10, 13))["asked"][0] == "Note 2"


def test_gap_question_wording() -> None:
    assert jobs._gap_question({"title": "WireGuard", "kind": "confirm", "detail": "Hosted on the gateway (to confirm)."}) == \
        "Is this right? Hosted on the gateway (to confirm)."
    assert jobs._gap_question({"title": "Frigate", "kind": "unwritten", "detail": "linked from Home Lab"}).startswith('What\'s "Frigate"?')


def test_health_snapshot_goes_on_the_health_note() -> None:
    siris = _siris()
    brain = siris.second_brain = FakeBrain()
    for offset in range(5):
        siris.health.record({"sleep_hours": 7.0 + offset * 0.1}, day=date(2026, 10, 2 + offset).isoformat())
    assert jobs.write_health_snapshot(siris, DAY) == {"written": "Health"}
    assert brain.learned[0]["title"] == "Health" and "Sleep:" in brain.learned[0]["content"]


def test_home_lab_log_summarises_the_week() -> None:
    siris = _siris()
    brain = siris.second_brain = FakeBrain()

    class FakeAudit:
        def since(self, since: datetime, limit: int = 500) -> list[dict]:
            return [
                {"tool_name": "docker_restart_container", "arguments": {"name": "plex"}, "error": None},
                {"tool_name": "docker_restart_container", "arguments": {"name": "plex"}, "error": None},
                {"tool_name": "unifi_list_devices", "arguments": {}, "error": "timeout"},
                {"tool_name": "weather_current", "arguments": {}, "error": "boom"},  # not home lab
            ]

        def recent_outcomes(self, since: datetime) -> dict:
            return {}

    siris.audit = FakeAudit()
    assert jobs.write_home_lab_log(siris, DAY, _at(18)) == {"written": "Home Lab"}
    content = brain.learned[0]["content"]
    assert "docker_restart_container plex ×2" in content and "unifi_list_devices ×1" in content
    assert "weather" not in content


def test_quiet_home_lab_week_writes_nothing() -> None:
    siris = _siris()
    siris.second_brain = FakeBrain()

    class QuietAudit:
        def since(self, since: datetime, limit: int = 500) -> list[dict]:
            return []

        def recent_outcomes(self, since: datetime) -> dict:
            return {}

    siris.audit = QuietAudit()
    assert jobs.write_home_lab_log(siris, DAY, _at(18)) == {"skipped": "a quiet week"}
