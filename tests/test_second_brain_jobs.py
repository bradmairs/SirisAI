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
    def __init__(self, notes: dict[str, str] | None = None, gaps: list[dict] | None = None,
                 memory: list[dict] | None = None, inbox_notes: list[dict] | None = None,
                 known: dict[str, str] | None = None) -> None:
        self.notes = notes or {}
        self._gaps = gaps or []
        self.learned: list[dict] = []
        self.memory = memory or []          # memory inbox entries
        self.inbox_notes = inbox_notes or []
        self.known = known or {}            # words the fake router recognises → the note they file to
        self.cleared: list[dict] = []
        self.file_calls: list[tuple[str, str, str]] = []
        self.split_calls: list[tuple[str, list[str]]] = []
        self.search_results: list[dict] = []
        self.activity_entries = [{"date": "2026-10-06", "line": "🧠 Learned [[Home Server]]"}]
        self.recall = {"score": 0.9, "found": 27, "total": 30, "misses": []}
        self.reviewed: list[str] = []
        self.queue = [{"title": "Plex", "path": "Notes/Plex.md", "links": 9, "as_of": "2026-10-05", "excerpt": "Media server."}]
        self.filed: list[str] = []
        self.relinked = 0
        self.sources: dict[str, str] = {}
        self.types: dict[str, str] = {}
        self.renamed: dict[str, str] = {}
        self.pages: list[dict] = []
        self.path = "/nonexistent-vault"

    def project_pages(self) -> list[dict]:
        return self.pages

    def inbox(self) -> dict:
        return {"memory": list(self.memory), "notes": list(self.inbox_notes)}

    def route(self, title: str, content: str, type: str = "auto") -> dict:
        for word, note in self.known.items():
            if content.startswith(word):
                return {"action": "update", "title": note}
        return {"action": "update", "title": jobs.MEMORY_INBOX_TITLE}

    def archive_split(self, title: str, into: list[str]) -> dict:
        self.split_calls.append((title, into))
        return {"action": "split", "title": title, "into": into, "path": f"04-Archive/Inbox/{title}.md"}

    def clear_memory_inbox(self, filed: list[dict]) -> dict:
        self.cleared += filed
        return {"archived": len(filed)}

    def file_note(self, title: str, type: str = "auto", into: str = "", folder: str = "", new_title: str = "") -> dict:
        if new_title:
            self.renamed[title] = new_title
        self.filed.append(title)
        self.file_calls.append((title, type, into) if not folder else (title, type, into, folder))
        if into:
            return {"action": "merged", "title": title, "into": into}
        if title == "Dom":
            return {"action": "merged", "title": title, "into": "Dom Mairs"}
        return {"action": "left" if title == "Hmm" else "filed", "title": title}

    def activity(self, days: int) -> list[dict]:
        return self.activity_entries

    def recall_check(self) -> dict:
        return self.recall

    def review_queue(self, count: int = 3) -> list[dict]:
        return self.queue[:count]

    def mark_reviewed(self, title: str) -> dict:
        self.reviewed.append(title)
        return {"reviewed": True, "title": title}

    def search(self, query: str, limit: int = 10) -> list[dict]:
        return self.search_results[:limit]

    def relink(self) -> int:
        self.relinked += 1
        return 2

    auto_linked: list[dict] = []

    def autolink(self) -> dict:
        return {"linked": list(self.auto_linked), "deferred": 0, "thresholds": {"auto_min": 0.8}}

    def insights(self, days: int = 30) -> dict:
        return {"highlights": ["You added 5 notes this week."], "deadlines": [], "overdue_tasks": [],
                "stale_projects": [], "inbox": {}}

    def learned_today(self, date: str) -> list[dict]:
        return [{"title": "Home Server", "action": "updated"}] if date == "2026-10-05" else []

    def learn(self, items: list[dict]) -> list[dict]:
        self.learned += items
        return [{"action": "learned", "title": i["title"], "path": f"Notes/{i['title']}.md", "links": []} for i in items]

    def get_note(self, title: str) -> dict | None:
        return {"title": title, "body": self.notes[title], "path": ""} if title in self.notes else None

    def gaps(self) -> list[dict]:
        return self._gaps

    def list_notes(self) -> list[dict]:
        return [{"title": t, "type": self.types.get(t, "note"), "aliases": [], "path": "", "source": self.sources.get(t, "")}
                for t in self.notes]


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
        self.priorities: list[str | None] = []

    def send(self, message: str, title: str = "", **kwargs: Any) -> None:
        self.sent.append((title, message))
        self.priorities.append(kwargs.get("priority"))

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


def test_jobs_run_once_a_day_from_their_hour() -> None:
    siris = _siris(brain_dream_hour_local=6, brain_weekly_hour_local=None, brain_upkeep_hour_local=None)
    siris.second_brain = FakeBrain()
    assert asyncio.run(jobs.run_due(siris, _at(5))) == {}          # before 06:00
    assert "dream" in asyncio.run(jobs.run_due(siris, _at(6)))
    assert asyncio.run(jobs.run_due(siris, _at(9))) == {}          # already ran today
    assert "dream" in asyncio.run(jobs.run_due(siris, _at(6, date(2026, 10, 7))))


def test_calendar_events_never_become_notes() -> None:
    # 2.66: the meeting-notes job is gone; a connected calendar writes nothing into the brain.
    siris = _siris(brain_dream_hour_local=None, brain_weekly_hour_local=None, brain_upkeep_hour_local=None)
    brain = siris.second_brain = FakeBrain()
    siris.tools.register(FakeCalendar([{"summary": "Call dad", "start": "2026-10-06T17:15:00+11:00", "calendar": "Google Calendar"}]))
    assert asyncio.run(jobs.run_due(siris, _at(12))) == {}
    assert brain.learned == [] and not hasattr(jobs, "write_meeting_notes")


def test_weekly_only_on_its_day() -> None:
    siris = _siris(brain_dream_hour_local=None, brain_upkeep_hour_local=None,
                   brain_weekly_day=1)
    siris.second_brain = FakeBrain()
    assert {"weekly", "review"} <= set(asyncio.run(jobs.run_due(siris, _at(18))))   # Tuesday = 1
    siris.state.set(jobs.STATE_KEY, {})
    assert asyncio.run(jobs.run_due(siris, _at(18, date(2026, 10, 7)))) == {}


def test_a_failing_job_is_reported_and_not_retried_today(monkeypatch) -> None:
    siris = _siris(brain_dream_hour_local=6, brain_weekly_hour_local=None, brain_upkeep_hour_local=None)
    siris.second_brain = FakeBrain()

    async def broken(*args: Any) -> Any:
        raise RuntimeError("model down")

    monkeypatch.setattr(jobs, "dream", broken)
    assert "model down" in asyncio.run(jobs.run_due(siris, _at(7)))["dream"]["error"]
    assert asyncio.run(jobs.run_due(siris, _at(8))) == {}


# ---------- dream ----------


def test_parse_notes_is_strict() -> None:
    text = 'Sure!\n```json\n{"notes": [{"title": "Home Server", "content": "Has 32 GB RAM.", "tags": ["Hardware"]},' \
           ' {"title": "", "content": "no title"}, {"title": "X", "content": "y", "type": "spaceship"}, "junk"]}\n```'
    assert jobs.parse_notes(text) == [   # no type given (or an unknown one): the vault's router decides
        {"title": "Home Server", "content": "Has 32 GB RAM.", "tags": ["hardware"]},
        {"title": "X", "content": "y", "tags": []},
    ]
    assert jobs.parse_notes('{"notes": [{"title": "Will", "content": "A friend.", "type": "person"}]}')[0]["type"] == "person"
    assert jobs.parse_notes("not json") == []
    assert jobs.parse_notes(None) == []
    many = '{"notes": [' + ",".join('{"title": "T%d", "content": "c"}' % i for i in range(20)) + "]}"
    assert len(jobs.parse_notes(many)) == 10


def test_day_material_leaves_the_memory_inbox_to_upkeep() -> None:
    siris = _siris()
    siris.second_brain = FakeBrain({jobs.MEMORY_INBOX_TITLE: "## Updates\n\n### 2026-10-06\nBrad prefers flat whites.\n"})
    assert "flat whites" not in jobs.day_material(siris, DAY)


def test_dream_turns_the_day_into_tagged_notes(monkeypatch: pytest.MonkeyPatch) -> None:
    siris = _siris()
    brain = siris.second_brain = FakeBrain({"Home Server": "i5"})
    monkeypatch.setattr(jobs, "day_material", lambda s, d: "Brad: I upgraded the server to 32 GB RAM.")
    siris.llm = FakeLLM('{"notes": [{"title": "Home Server", "content": "Upgraded to 32 GB RAM."}]}')
    result = asyncio.run(jobs.dream(siris, DAY))
    assert result == {"learned": ["Home Server"]}
    assert brain.learned[0]["tags"] == ["siris-dream"]
    assert brain.learned[0]["source"] == "SirisAI conversations, 2026-10-06"
    assert brain.learned[0]["inbox_if_new"] is True   # new notes are sorted by the upkeep
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
    assert jobs.write_health_snapshot(siris, DAY)["written"] == "Health"
    assert brain.learned[0]["title"] == "Health" and "a night" in brain.learned[0]["content"]


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


# ---------- upkeep ----------


def _entry(i: str, text: str) -> dict:
    return {"id": i, "date": "2026-10-05", "text": text, "plain": text}


def test_upkeep_runs_nightly_at_its_hour() -> None:
    siris = _siris(brain_dream_hour_local=None, brain_weekly_hour_local=None)
    siris.second_brain = FakeBrain()
    assert asyncio.run(jobs.run_due(siris, _at(1))) == {}
    assert "upkeep" in asyncio.run(jobs.run_due(siris, _at(2)))
    assert asyncio.run(jobs.run_due(siris, _at(4))) == {}


def test_memories_go_where_the_router_or_the_model_says() -> None:
    siris = _siris()
    brain = siris.second_brain = FakeBrain(
        {"Dom Mairs": "wife", "Coffee": "x"},
        memory=[_entry("a1", "Dom loves trail running."), _entry("b2", "Brad drinks flat whites."),
                _entry("c3", "ok thanks")],
        known={"Dom": "Dom Mairs"},
    )
    siris.llm = FakeLLM('{"filed": [{"id": "b2", "title": "Coffee"}, {"id": "zz", "title": "Made Up"}]}')
    result = asyncio.run(jobs.file_memory_inbox(siris))
    assert result == {"filed": ["Dom Mairs", "Coffee"], "left": 1}
    assert [(i["title"], i["content"]) for i in brain.learned] == [
        ("Dom Mairs", "Dom loves trail running."), ("Coffee", "Brad drinks flat whites.")]
    assert brain.learned[0]["source"] == "Siris memory, 2026-10-05"
    assert brain.cleared == [{"id": "a1", "filed_to": "Dom Mairs"}, {"id": "b2", "filed_to": "Coffee"}]
    prompt = siris.llm.messages[1]["content"]
    assert "id b2" in prompt and "id a1" not in prompt   # the router's picks never reach the model


def test_without_a_model_only_the_routers_picks_are_filed() -> None:
    siris = _siris()
    siris.llm = None
    brain = siris.second_brain = FakeBrain(memory=[_entry("a1", "Dom runs."), _entry("b2", "Something.")], known={"Dom": "Dom Mairs"})
    assert asyncio.run(jobs.file_memory_inbox(siris)) == {"filed": ["Dom Mairs"], "left": 1}
    assert [c["id"] for c in brain.cleared] == ["a1"]


def test_without_a_model_inbox_notes_are_filed_by_the_router() -> None:
    siris = _siris()
    siris.llm = None
    brain = siris.second_brain = FakeBrain(inbox_notes=[{"title": t} for t in ("Knee Soreness", "Dom", "Hmm")])
    assert asyncio.run(jobs.file_inbox_notes(siris)) == {"filed": ["Knee Soreness"], "merged": ["Dom Mairs"], "left": ["Hmm"], "split": []}
    assert brain.filed == ["Knee Soreness", "Dom", "Hmm"]


def _sort(reply: str, title: str = "SirisHome Business") -> tuple[FakeBrain, dict, FakeLLM]:
    siris = _siris()
    brain = siris.second_brain = FakeBrain(inbox_notes=[{"title": title, "text": "Offer local AI installs to homeowners."}])
    brain.search_results = [
        {"title": title, "type": "capture", "path": f"00-Inbox/{title}.md", "excerpts": ["itself"]},
        {"title": "SirisHome", "type": "note", "path": "Notes/SirisHome.md", "excerpts": ["The smart home business."]},
        {"title": "Health", "type": "area", "path": "02-Areas/Health.md", "excerpts": ["hub"]},
        {"title": "Old Capture", "type": "note", "path": "04-Archive/Inbox/Old Capture.md", "excerpts": ["x"]},
    ]
    siris.llm = FakeLLM(reply)
    return brain, asyncio.run(jobs.file_inbox_notes(siris)), siris.llm


def test_the_model_merges_an_inbox_note_into_a_note_on_the_same_topic() -> None:
    brain, out, llm = _sort('{"action": "merge", "into": "SirisHome"}')
    assert out == {"filed": [], "merged": ["SirisHome"], "left": [], "split": []}
    assert brain.file_calls == [("SirisHome Business", "auto", "SirisHome")]
    prompt = llm.messages[1]["content"]
    assert "- SirisHome (note): The smart home business." in prompt
    assert "Health" not in prompt and "Old Capture" not in prompt and "(capture)" not in prompt   # not merge targets


def test_the_model_can_only_merge_into_a_note_the_search_found() -> None:
    brain, out, _ = _sort('{"action": "merge", "into": "Health"}')
    assert brain.file_calls == [("SirisHome Business", "auto", "")]   # back to the router


def test_the_model_files_new_topics_with_a_type_or_leaves_them() -> None:
    brain, out, _ = _sort('{"action": "new", "type": "project"}')
    assert brain.file_calls == [("SirisHome Business", "project", "")] and out["filed"] == ["SirisHome Business"]
    brain, out, _ = _sort('{"action": "new", "type": "spaceship"}')
    assert brain.file_calls == [("SirisHome Business", "auto", "")]
    brain, out, _ = _sort('{"action": "leave"}')
    assert brain.file_calls == [] and out["left"] == ["SirisHome Business"]
    brain, out, _ = _sort("no idea")
    assert brain.file_calls == [("SirisHome Business", "auto", "")]


CAPTURE = ("Had lunch with [[Dad]] today, he's retiring next year. Leg day at the gym: squats up to 100 kg.\n\n"
           "Thinking the homelab needs a second NAS for backups. And the Siris App Suite should get a launcher on the phone.")


def _split(parts: list[dict]) -> tuple[FakeBrain, dict, FakeLLM]:
    import json as _json

    siris = _siris()
    brain = siris.second_brain = FakeBrain(inbox_notes=[{"title": "Had lunch with Dad today, he's retiring…", "text": CAPTURE}])
    brain.search_results = [
        {"title": "Dad", "type": "person", "path": "People/Dad.md", "excerpts": ["My dad, Rob."]},
        {"title": "Home Lab", "type": "note", "path": "Notes/Home Lab.md", "excerpts": ["The server and NAS."]},
        {"title": "Siris App Suite", "type": "project", "path": "01-Projects/Siris App Suite.md", "excerpts": ["The apps."]},
    ]
    siris.llm = FakeLLM(_json.dumps({"action": "split", "parts": parts}))
    return brain, asyncio.run(jobs.file_inbox_notes(siris)), siris.llm


def test_a_capture_about_several_things_is_split_across_their_notes() -> None:
    brain, out, llm = _split([
        {"text": "Had lunch with Dad today, he's retiring next year.", "into": "Dad"},
        {"text": "Leg day at the gym: squats up to 100 kg.", "title": "Gym Training", "type": "note"},
        {"text": "Thinking the homelab needs a second NAS for backups.", "into": "Home Lab"},
        {"text": "And the Siris App Suite should get a launcher on the phone.", "into": "Siris App Suite"},
    ])
    assert [(i["title"], i["content"]) for i in brain.learned] == [
        ("Dad", "Had lunch with Dad today, he's retiring next year."),
        ("Gym Training", "Leg day at the gym: squats up to 100 kg."),
        ("Home Lab", "Thinking the homelab needs a second NAS for backups."),
        ("Siris App Suite", "And the Siris App Suite should get a launcher on the phone."),
    ]
    assert brain.learned[1]["type"] == "note" and "type" not in brain.learned[0]
    assert all(i["source"] == "Capture: Had lunch with Dad today, he's retiring…" for i in brain.learned)
    assert all(i["not_link"] == ["Had lunch with Dad today, he's retiring…"] for i in brain.learned)
    assert brain.split_calls == [("Had lunch with Dad today, he's retiring…", ["Dad", "Gym Training", "Home Lab", "Siris App Suite"])]
    assert brain.file_calls == []   # archived by the split, not filed whole
    assert out["split"] == ["Had lunch with Dad today, he's retiring… → Dad, Gym Training, Home Lab, Siris App Suite"]
    assert "Split 1 capture across the notes it was about" in jobs._upkeep_message({"inbox": out})
    # Each paragraph was searched too, so the homelab and the app suite had their notes in front of the model.
    assert "- Home Lab (note)" in llm.messages[1]["content"] and "- Siris App Suite (project)" in llm.messages[1]["content"]


def test_a_split_only_writes_the_notes_own_words_into_notes_the_search_found() -> None:
    brain, out, _ = _split([
        {"text": "Dad is retiring next year and moving to Bali.", "into": "Dad"},          # reworded and embellished
        {"text": "Leg day at the gym: squats up to 100 kg.", "into": "Health"},           # not a candidate, no title
        {"text": "Thinking the homelab needs a second NAS for backups.", "into": "Home Lab"},
        {"text": "And the Siris App Suite should get a launcher on the phone.", "into": "Siris App Suite"},
    ])
    assert all("Bali" not in i["content"] and i["title"] != "Health" for i in brain.learned)
    # What's left holds only half the note, so it's filed whole rather than half-archived.
    assert brain.learned == [] and brain.split_calls == []
    assert brain.file_calls == [("Had lunch with Dad today, he's retiring…", "auto", "")]


def test_a_split_that_leaves_most_of_the_note_out_files_it_whole() -> None:
    brain, out, _ = _split([
        {"text": "Leg day at the gym: squats up to 100 kg.", "title": "Gym Training"},
        {"text": "And the Siris App Suite should get a launcher on the phone.", "into": "Siris App Suite"},
    ])
    assert brain.learned == [] and brain.split_calls == []   # Dad and the homelab would only be in the archive
    assert brain.file_calls == [("Had lunch with Dad today, he's retiring…", "auto", "")]


def test_a_note_the_inbox_cut_short_is_never_split() -> None:
    import json as _json

    siris = _siris()
    brain = siris.second_brain = FakeBrain(inbox_notes=[{"title": "Long OneNote page", "text": CAPTURE, "chars": 20000}])
    brain.search_results = [{"title": "Dad", "type": "person", "path": "People/Dad.md", "excerpts": [""]},
                            {"title": "Home Lab", "type": "note", "path": "Notes/Home Lab.md", "excerpts": [""]}]
    siris.llm = FakeLLM(_json.dumps({"action": "split", "parts": [
        {"text": "Had lunch with Dad today, he's retiring next year.", "into": "Dad"},
        {"text": "Thinking the homelab needs a second NAS for backups.", "into": "Home Lab"}]}))
    asyncio.run(jobs.file_inbox_notes(siris))
    assert brain.split_calls == [] and brain.file_calls == [("Long OneNote page", "auto", "")]


def test_the_model_sees_a_long_page_whole() -> None:
    siris = _siris()
    page = "Site visit notes. " * 400 + "The last line about seals."
    siris.second_brain = FakeBrain(inbox_notes=[{"title": "Site Visit", "text": page}])
    siris.llm = FakeLLM('{"action": "new"}')
    asyncio.run(jobs.file_inbox_notes(siris))
    assert "The last line about seals." in siris.llm.messages[1]["content"]


def test_a_split_with_one_usable_piece_files_the_note_whole() -> None:
    brain, out, _ = _split([
        {"text": "Thinking the homelab needs a second NAS for backups.", "into": "Home Lab"},
        {"text": "Something it never said.", "into": "Dad"},
    ])
    assert brain.learned == [] and brain.split_calls == []
    assert brain.file_calls == [("Had lunch with Dad today, he's retiring…", "auto", "")]


def test_repos_map_to_their_project_notes() -> None:
    notes = [
        {"title": "GVW Timesheet Tool", "type": "project", "aliases": [], "source": ""},
        {"title": "Siris Engineering Archive", "type": "project", "aliases": [], "source": ""},
        {"title": "SirisOS", "type": "project", "aliases": [], "source": "https://github.com/bradmairs/SirisOS"},
        {"title": "Dom Mairs", "type": "person", "aliases": ["Dom"], "source": ""},
    ]
    repo = lambda name: {"repo": name, "url": f"https://github.com/bradmairs/{name}"}  # noqa: E731
    assert jobs.project_note_for(repo("gvw-timesheet-tool"), notes) == "GVW Timesheet Tool"
    assert jobs.project_note_for(repo("Engineering-Archive"), notes) == "Siris Engineering Archive"
    assert jobs.project_note_for(repo("SirisOS"), notes) == "SirisOS"
    assert jobs.project_note_for(repo("dom"), notes) is None   # people aren't projects
    assert jobs.project_note_for(repo("brand-new-thing"), notes) is None
    assert jobs._repo_title("brand-new-thing") == "Brand New Thing" and jobs._repo_title("SirisAI") == "SirisAI"


def _github(monkeypatch: pytest.MonkeyPatch, activity: list[dict]) -> list[datetime]:
    seen: list[datetime] = []

    class FakeGitHub:
        def __init__(self, token: str, owner: str) -> None:
            assert token == "tok" and owner == "bradmairs"

        def repos_pushed_since(self, since: datetime) -> list[dict]:
            seen.append(since)
            return [{"name": a["repo"]} for a in activity]

        def activity(self, repo: dict, since: datetime) -> dict:
            return next(a for a in activity if a["repo"] == repo["name"])

        def close(self) -> None:
            pass

    import app.second_brain.github as github
    monkeypatch.setattr(github, "GitHubActivityClient", FakeGitHub)
    return seen


def _act(repo: str, prs: int = 0, commits: int = 0) -> dict:
    return {"repo": repo, "full_name": f"bradmairs/{repo}", "url": f"https://github.com/bradmairs/{repo}", "description": "",
            "merged_prs": [{"number": i + 1, "title": f"PR {i + 1}", "body": "", "url": f"https://github.com/bradmairs/{repo}/pull/{i + 1}"}
                           for i in range(prs)],
            "commits": [{"sha": "abc", "message": f"commit {i}", "url": ""} for i in range(commits)]}


def test_github_activity_updates_project_notes(monkeypatch: pytest.MonkeyPatch) -> None:
    siris = _siris(brain_github_token="tok")
    brain = siris.second_brain = FakeBrain({"SirisAI": "x"})
    brain.types["SirisAI"] = "project"
    seen = _github(monkeypatch, [_act("SirisAI", prs=1), _act("tiny", commits=1), _act("New-Tool", prs=1),
                                 _act("Siris-Second-Brain", commits=9)])
    siris.llm = FakeLLM("Nightly brain upkeep now runs on the local model.")
    now = _at(2)
    result = asyncio.run(jobs.github_activity(siris, now))
    assert result["updated"] == ["SirisAI", "New Tool"] and result["repos_checked"] == 3
    first, new = brain.learned
    assert first == {"title": "SirisAI", "content": "Nightly brain upkeep now runs on the local model.",
                     "tags": ["github"], "source": "https://github.com/bradmairs/SirisAI/pull/1"}
    assert new["type"] == "project" and "Code: https://github.com/bradmairs/New-Tool" in new["content"]
    assert seen[0] == now - jobs.timedelta(hours=26)
    asyncio.run(jobs.github_activity(siris, _at(2, date(2026, 10, 7))))
    assert seen[1] == now   # the next sweep starts where this one ended


def test_github_noise_is_skipped_and_no_model_lists_prs(monkeypatch: pytest.MonkeyPatch) -> None:
    siris = _siris(brain_github_token="tok")
    brain = siris.second_brain = FakeBrain({"SirisAI": "x", "SirisOS": "y"})
    _github(monkeypatch, [_act("SirisAI", prs=2)])
    siris.llm = FakeLLM("NOTHING")
    assert asyncio.run(jobs.github_activity(siris, _at(2)))["updated"] == []
    siris.llm = None
    asyncio.run(jobs.github_activity(siris, _at(3)))
    assert brain.learned[0]["content"] == "Merged: PR 1 (#1); PR 2 (#2)."


def test_github_needs_a_token() -> None:
    siris = _siris()
    siris.second_brain = FakeBrain()
    assert asyncio.run(jobs.github_activity(siris, _at(2))) == {"skipped": "no GitHub token"}


def test_upkeep_reports_once_and_keeps_going_past_a_failure() -> None:
    siris = _siris()
    brain = siris.second_brain = FakeBrain({"Dom Mairs": "wife"}, memory=[_entry("a1", "Dom runs.")],
                                           inbox_notes=[{"title": "Hmm"}], known={"Dom": "Dom Mairs"})
    siris.llm = None
    siris._notify_client = FakeNotify()

    def broken() -> dict:
        raise RuntimeError("disk full")

    brain.file_note = lambda *a, **k: broken()  # type: ignore[method-assign]
    out = asyncio.run(jobs.upkeep(siris, _at(2)))
    assert out["memory"]["filed"] == ["Dom Mairs"] and "disk full" in out["inbox"]["error"]
    assert out["github"] == {"skipped": "no GitHub token"} and out["relink"] == 2
    title, message = siris._notify_client.sent[0]
    assert title == "Siris: brain upkeep"
    assert "Filed 1 memories (Dom Mairs)." in message and "Failed: inbox." in message


def test_a_quiet_night_sends_a_low_priority_summary() -> None:
    siris = _siris()
    siris.second_brain = FakeBrain()
    siris._notify_client = FakeNotify()
    siris.state.set(jobs.LAST_DREAM_KEY, {"date": "2026-10-05", "learned": 0, "note": "nothing worth keeping"})
    asyncio.run(jobs.upkeep(siris, _at(2)))
    (title, message), = siris._notify_client.sent
    assert title == "Siris: brain upkeep" and siris._notify_client.priorities == ["low"]
    assert message.startswith("Learned or updated 1 notes in the last day. The dream found nothing worth keeping.")
    assert "Recall 90%." in message


def test_three_quiet_days_raise_an_alert() -> None:
    siris = _siris()
    brain = siris.second_brain = FakeBrain()
    brain.activity_entries = [{"date": "2026-10-05", "line": "🔗 Linked [[A]] ↔ [[B]]"}]   # links aren't learning
    siris._notify_client = FakeNotify()
    out = asyncio.run(jobs.upkeep(siris, _at(2)))
    assert out["growth"]["quiet"] is True
    title, message = siris._notify_client.sent[0]
    assert title == "Siris: brain needs a look" and siris._notify_client.priorities == ["high"]
    assert "hasn't learned anything in 3 days" in message


def test_a_recall_drop_raises_an_alert() -> None:
    siris = _siris()
    brain = siris.second_brain = FakeBrain()
    siris._notify_client = FakeNotify()
    siris.state.set(jobs.LAST_RECALL_KEY, 0.9)
    brain.recall = {"score": 0.8, "found": 24, "total": 30, "misses": [{"q": "what car do I drive?"}]}
    out = asyncio.run(jobs.upkeep(siris, _at(2)))
    assert out["recall"] == {"score": 0.8, "previous": 0.9, "found": 24, "total": 30, "misses": ["what car do I drive?"]}
    assert "Recall dropped from 90% to 80%" in siris._notify_client.sent[0][1]
    assert siris.state.get(jobs.LAST_RECALL_KEY) == 0.8


def test_claude_chats_are_distilled_then_kept_as_a_log() -> None:
    siris = _siris()
    chat = {"title": "Claude Chat 2026-10-17 – Mac mini sizing", "source": "Claude chat", "text": "User: 32 or 64 GB?"}
    brain = siris.second_brain = FakeBrain({"Local AI Hardware": "x"}, inbox_notes=[chat, {"title": "Garden", "source": ""}])
    siris.llm = FakeLLM('{"notes": [{"title": "Local AI Hardware", "content": "Brad leans to 32 GB."}]}')
    out = asyncio.run(jobs.distill_chats(siris))
    assert out == {"chats": [chat["title"]], "learned": ["Local AI Hardware"]}
    item = brain.learned[0]
    assert item["inbox_if_new"] is True and item["links"] == [chat["title"]] and "claude-chat" in item["tags"]
    assert brain.file_calls == [(chat["title"], "resource", "", jobs.CHATS_FOLDER)]


def test_without_a_model_chats_wait_and_are_never_filed_raw() -> None:
    siris = _siris()
    siris.llm = None
    chat = {"title": "Claude Chat – x", "source": "Claude chat", "text": "..."}
    brain = siris.second_brain = FakeBrain(inbox_notes=[chat])
    assert asyncio.run(jobs.distill_chats(siris)) == {"chats": [], "learned": []}
    assert asyncio.run(jobs.file_inbox_notes(siris)) == {"filed": [], "merged": [], "left": [], "split": []}
    assert brain.file_calls == []


def test_weekly_review_prompt_and_gap_questions_skip_files() -> None:
    siris = _siris(brain_gap_questions=5)
    brain = siris.second_brain = FakeBrain(gaps=[{"title": "shot.png", "kind": "missing_attachment", "detail": "in X"},
                                                 {"title": "Frigate", "kind": "unwritten", "detail": "linked from Home Lab"}])
    siris._notify_client = FakeNotify()
    assert jobs.ask_to_review_notes(siris) == {"asked": ["Plex"]}
    assert "1. Plex (as of 2026-10-05): Media server." in siris._notify_client.sent[0][1]
    assert jobs.ask_gap_questions(siris, DAY) == {"asked": ["Frigate"]}


def test_weekly_review_note() -> None:
    siris = _siris()
    brain = siris.second_brain = FakeBrain()
    siris.llm = FakeLLM("## This week\n- Upgraded the server.")
    assert asyncio.run(jobs.weekly_review(siris, DAY)) == {"written": "2026-W41-review"}
    item = brain.learned[0]
    assert item["folder"] == "03-Resources/Weekly Reviews" and item["tags"] == ["weekly-review"] and "Upgraded" in item["content"]
    assert "Home Server" in siris.llm.messages[1]["content"]
    brain.notes["2026-W41-review"] = "done"
    assert asyncio.run(jobs.weekly_review(siris, DAY)) == {"skipped": "already written"}


def test_github_client_reads_pushed_repos_merged_prs_and_commits() -> None:
    import httpx

    from app.second_brain.github import GitHubActivityClient

    since = datetime(2026, 10, 5, tzinfo=UTC)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer tok"
        path = request.url.path
        if path == "/user/repos":
            return httpx.Response(200, json=[
                {"name": "SirisAI", "full_name": "bradmairs/SirisAI", "html_url": "https://github.com/bradmairs/SirisAI",
                 "owner": {"login": "bradmairs"}, "pushed_at": "2026-10-05T10:00:00Z", "fork": False, "archived": False},
                {"name": "old", "owner": {"login": "bradmairs"}, "pushed_at": "2026-09-01T00:00:00Z"},
                {"name": "forked", "owner": {"login": "bradmairs"}, "pushed_at": "2026-10-05T10:00:00Z", "fork": True},
            ])
        if path.endswith("/commits"):
            assert request.url.params["since"] == "2026-10-05T00:00:00Z"
            return httpx.Response(200, json=[
                {"sha": "abcdef123", "commit": {"message": "Add upkeep\n\ndetails"}, "html_url": "c1", "parents": [{}]},
                {"sha": "fffffff00", "commit": {"message": "Merge pull request #62"}, "html_url": "c2", "parents": [{}, {}]},
            ])
        return httpx.Response(200, json=[
            {"number": 62, "title": "Upkeep", "body": "Runs nightly", "html_url": "p62", "merged_at": "2026-10-05T09:00:00Z"},
            {"number": 61, "title": "Old", "html_url": "p61", "merged_at": "2026-10-01T09:00:00Z"},
            {"number": 60, "title": "Closed", "html_url": "p60", "merged_at": None},
        ])

    client = GitHubActivityClient("tok", "bradmairs", transport=httpx.MockTransport(handler))
    repos = client.repos_pushed_since(since)
    assert [r["name"] for r in repos] == ["SirisAI"]
    a = client.activity(repos[0], since)
    assert a["commits"] == [{"sha": "abcdef1", "message": "Add upkeep", "url": "c1"}]
    assert [p["number"] for p in a["merged_prs"]] == [62]
    client.close()


def test_upkeep_auto_links_confident_pairs_and_says_so() -> None:
    siris = _siris()
    brain = siris.second_brain = FakeBrain()
    brain.auto_linked = [{"a": "Pump Stations", "b": "Wet Wells", "confidence": 91},
                         {"a": "Valves", "b": "Rising Mains", "confidence": 84}]
    siris.llm = None
    siris._notify_client = FakeNotify()
    out = asyncio.run(jobs.upkeep(siris, _at(2)))
    assert len(out["autolink"]["linked"]) == 2
    assert "Linked 2 pairs of notes it was sure about." in siris._notify_client.sent[0][1]


# ---------- capture titles, photos, project pages ----------


def test_a_cut_off_capture_title_is_replaced_when_filed() -> None:
    brain, out, _ = _sort('{"action": "new", "type": "note", "title": "Where I Capture Things in Siris"}',
                          title="I’ve decided I want to add things to…")
    assert brain.renamed == {"I’ve decided I want to add things to…": "Where I Capture Things in Siris"}
    brain, out, _ = _sort('{"action": "new", "title": "A Different Name"}')   # a real title stays
    assert brain.renamed == {}
    brain, out, _ = _sort('{"action": "new", "title": "Still cut off…"}', title="Photo 2026-10-21 08:14")
    assert brain.renamed == {}


class FakeVision:
    def __init__(self, reply: str = "# Site markup\n- [ ] Move valve 300 mm east") -> None:
        self.reply, self.calls = reply, 0

    def describe(self, images: list[bytes], prompt: str | None = None) -> str:
        self.calls += 1
        return self.reply


def test_photos_in_the_inbox_are_read_once(tmp_path: Any) -> None:
    (tmp_path / "Attachments").mkdir()
    (tmp_path / "Attachments" / "2026-10-21 board.jpg").write_bytes(b"\xff\xd8\xff photo")
    (tmp_path / "00-Inbox").mkdir()
    (tmp_path / "00-Inbox" / "Photo 2026-10-21 08:14.md").write_text(
        "---\ntype: capture\n---\n# Photo 2026-10-21 08:14\n\n![[Attachments/2026-10-21 board.jpg]]\n")
    siris = _siris()
    siris.state.set(jobs.PHOTO_KEY, [])
    brain = siris.second_brain = FakeBrain(inbox_notes=[{"title": "Photo 2026-10-21 08:14", "path": "00-Inbox/Photo 2026-10-21 08:14.md"}])
    brain.path = str(tmp_path)
    siris.vision = FakeVision()
    assert asyncio.run(jobs.read_inbox_photos(siris)) == {"read": ["Photo 2026-10-21 08:14"]}
    assert "Move valve 300 mm east" in brain.learned[0]["content"]
    assert brain.learned[0]["title"] == "Photo 2026-10-21 08:14"
    assert asyncio.run(jobs.read_inbox_photos(siris)) == {"read": []}   # never twice
    assert siris.vision.calls == 1
    siris.vision = None
    assert "skipped" in asyncio.run(jobs.read_inbox_photos(siris))


def test_the_upkeep_summary_mentions_photos_pages_and_emails() -> None:
    message = jobs._upkeep_message({
        "photos": {"read": ["Photo 1"]},
        "projects": [{"project": "Melrose East Pump Station", "action": "updated"}, {"project": "Other", "action": "unchanged"}],
        "gmail": {"imported": ["Booking", "Receipt"]},
    })
    assert "Read 1 photo in the inbox." in message
    assert "Project pages refreshed: Melrose East Pump Station." in message
    assert "Brought in 2 emails labelled Siris." in message


# ---------- action items and CPEng evidence ----------

NOW_OCT = datetime(2026, 10, 29, 14, 0, tzinfo=UTC)


def test_recent_imports_get_their_action_items_and_old_ones_dont() -> None:
    siris = _siris()
    siris.state.set(jobs.ACTIONS_KEY, [])
    brain = siris.second_brain = FakeBrain(inbox_notes=[
        {"title": "Pump quote", "path": "00-Inbox/Pump quote.md", "source": "Email from Dave, 2026-10-28",
         "text": "*Emailed to Siris: from Dave, 2026-10-28.*\n\nPlease send the revised quote by Friday 31 October."},
        {"title": "Old meeting", "path": "00-Inbox/Old meeting.md", "source": "OneNote: NZDF › Meetings › Old meeting",
         "text": "*From OneNote: NZDF › Meetings, created 2021-11-22.*\n\nTony to send consents."},
        {"title": "A thought", "path": "00-Inbox/A thought.md", "source": "", "text": "Maybe paint the fence."},
    ])
    siris.llm = FakeLLM('{"tasks": [{"task": "Send Dave the revised quote", "due": "2026-10-31"}, {"task": "", "due": null}]}')
    out = asyncio.run(jobs.extract_actions(siris, NOW_OCT))
    assert out == {"tasks": {"Pump quote": 1}}
    assert brain.learned[0]["title"] == "Pump quote"
    assert "- [ ] Send Dave the revised quote 📅 2026-10-31" in brain.learned[0]["content"]
    assert asyncio.run(jobs.extract_actions(siris, NOW_OCT)) == {"tasks": {}}   # each note is read once
    siris.llm = None
    assert "skipped" in asyncio.run(jobs.extract_actions(siris, NOW_OCT))


class CPEngBrain(FakeBrain):
    def __init__(self) -> None:
        super().__init__(notes={"CPEng Chartership": "# CPEng\n## Evidence from your notes\n"
                                                     "- [[Civil 3D Surfaces]]: 12 Advanced engineering knowledge. Software steps.\n",
                                "Dellows Underslip – Outlet Options": "Chose riprap over a drop structure.",
                                "Water Asset Forum – Tina Porou": "Talk notes.", "Civil 3D Surfaces": "How to build a surface.",
                                "Box Culverts": "riprap guidance from a supplier"})
        self.lines: list[tuple[str, str, str]] = []
        self.removed: list[str] = []
        self.blocks: dict[str, str] = {}

    def list_notes(self) -> list[dict]:
        return [{"title": "CPEng Chartership", "type": "project", "path": "01-Projects/CPEng Chartership.md"},
                {"title": "Dellows Underslip – Outlet Options", "type": "note", "path": "Work/WSP/Dellows Underslip – Outlet Options.md"},
                {"title": "Dellows Underslip", "type": "project", "path": "Work/WSP/Projects/Dellows Underslip.md"},
                {"title": "Water Asset Forum – Tina Porou", "type": "note", "path": "Work/Professional Development/x.md"},
                {"title": "Civil 3D Surfaces", "type": "note", "path": "Work/Civil 3D/Civil 3D Surfaces.md"},
                {"title": "Box Culverts", "type": "note", "path": "Work/Box Culverts.md"}]

    def _cpeng(self) -> str:
        return self.notes["CPEng Chartership"]

    def add_line(self, note: str, heading: str, line: str) -> dict:
        self.lines.append((note, heading, line))
        self.notes[note] += line + "\n"
        return {"action": "added", "title": note}

    def remove_line(self, note: str, heading: str, starts: str) -> dict:
        self.removed.append(starts)
        self.notes[note] = "\n".join(l for l in self.notes[note].split("\n") if not l.startswith(starts))
        return {"action": "removed", "title": note, "removed": 1}

    def set_block(self, note: str, name: str, text: str) -> dict:
        self.blocks[name] = text
        return {"action": "set", "title": note}


class SwitchLLM:
    async def generate(self, messages: list[dict], **kwargs: Any) -> LLMResponse:
        if "riprap" in messages[-1]["content"]:
            return LLMResponse(text='{"evidence": true, "elements": [4, 14, 99], "why": "He compared outlet options and picked riprap."}')
        return LLMResponse(text='{"evidence": false, "elements": [], "why": ""}')


def test_work_notes_that_show_competencies_are_listed_on_the_cpeng_note() -> None:
    siris = _siris()
    siris.state.set(jobs.CPENG_KEY, [])
    siris.state.set(jobs.CPENG_REJECTED_KEY, ["Box Culverts"])
    brain = siris.second_brain = CPEngBrain()
    siris.llm = SwitchLLM()
    out = asyncio.run(jobs.cpeng_evidence(siris))
    # the line already listed is re-checked first and comes off; project pages and rejected notes aren't read
    assert out == {"added": ["Dellows Underslip – Outlet Options"], "removed": ["Civil 3D Surfaces"], "read": 3}
    assert brain.lines == [("CPEng Chartership", "## Evidence from your notes",
                            "- [[Dellows Underslip – Outlet Options]]: 4 Develop safe and sustainable solutions, "
                            "14 Problem analysis. He compared outlet options and picked riprap.")]
    assert "Civil 3D" not in brain.notes["CPEng Chartership"]
    coverage = brain.blocks["cpeng-coverage"]
    assert "| 4. Develop safe and sustainable solutions | 1 |" in coverage
    assert "| 8. Communication | none yet |" in coverage and "**No evidence yet:**" in coverage
    assert asyncio.run(jobs.cpeng_evidence(siris)) == {"added": [], "removed": [], "read": 0}


def test_brad_can_say_a_note_is_not_evidence() -> None:
    from app.plugins.builtin.second_brain import BrainNotCPEngEvidenceTool

    siris = _siris()
    siris.state.set(jobs.CPENG_REJECTED_KEY, [])
    brain = CPEngBrain()
    out = BrainNotCPEngEvidenceTool(brain, siris.state).run(title="Civil 3D Surfaces")
    assert out == {"action": "not-evidence", "title": "Civil 3D Surfaces", "removed": 1}
    assert brain.removed == ["- [[Civil 3D Surfaces]]"]
    assert siris.state.get(jobs.CPENG_REJECTED_KEY) == ["Civil 3D Surfaces"]


def test_the_weekly_check_lists_each_note_with_its_date() -> None:
    siris = _siris(brain_review_notes=2)
    brain = siris.second_brain = FakeBrain()
    brain.queue = [{"title": "Site Notes", "as_of": "2026-10-20", "excerpt": "Riprap", "handwriting": True}]
    siris._notify_client = FakeNotify()
    jobs.ask_to_review_notes(siris)
    assert "1. Site Notes (as of 2026-10-20): Riprap" in siris._notify_client.sent[0][1]


# ---------- topic summaries, CPEng drafts ----------


class PagesBrain(FakeBrain):
    def __init__(self) -> None:
        super().__init__(notes={
            "Culverts": "**[[Buller SIP – Culvert Recommendations]]** (WSP, 2023-02-16)\n- Culvert 77: insufficient depth.",
            "Drinking Water": "Nothing in your work notes mentions it yet.",
            "CPEng Chartership": "## Evidence from your notes\n"
                                 "- [[Dellows Outlet]]: 4 Develop safe and sustainable solutions, 14 Problem analysis. Picked riprap.\n"
                                 "- [[Buller Culverts]]: 14 Problem analysis. Assessed culvert depth.\n"
                                 "- [[Site Visit]]: 4 Develop safe and sustainable solutions. One-off.\n",
            "Dellows Outlet": "Chose riprap over a drop structure.", "Buller Culverts": "Culvert 77 too shallow.", "Site Visit": "x",
        })
        self.blocks: list[tuple[str, str, str]] = []

    def list_notes(self) -> list[dict]:
        return [{"title": "Culverts", "type": "resource", "path": "Work/Topics/Culverts.md"},
                {"title": "Drinking Water", "type": "resource", "path": "Work/Topics/Drinking Water.md"},
                {"title": "Dellows Outlet", "type": "note", "path": "Work/WSP/Dellows Outlet.md"}]

    def set_block(self, note: str, name: str, text: str) -> dict:
        self.blocks.append((note, name, text))
        return {"action": "set", "title": note}


def test_topic_pages_get_a_weekly_summary() -> None:
    siris = _siris()
    brain = siris.second_brain = PagesBrain()
    siris.llm = FakeLLM("Here you go:\n- Shallow culverts kept needing replacement ([[Buller SIP – Culvert Recommendations]]).")
    out = asyncio.run(jobs.topic_summaries(siris))
    assert out == {"summarised": ["Culverts"]}   # an empty topic is left alone
    note, name, text = brain.blocks[0]
    assert (note, name) == ("Culverts", "topic-summary")
    assert text.startswith("## Summary\n*Written by Siris") and "- Shallow culverts" in text and "Here you go" not in text


def test_cpeng_elements_with_two_pieces_of_evidence_get_a_draft_once() -> None:
    siris = _siris()
    siris.state.set(jobs.CPENG_DRAFTS_KEY, {})
    brain = siris.second_brain = PagesBrain()
    siris.llm = FakeLLM("On Dellows Underslip I compared outlet options and chose riprap [check: date].")
    out = asyncio.run(jobs.cpeng_drafts(siris))
    assert out == {"drafted": ["4. Develop safe and sustainable solutions", "14. Problem analysis"]}
    assert brain.learned[0]["title"] == jobs.CPENG_DRAFTS_NOTE   # the drafts note is made once it's needed
    note, name, text = brain.blocks[0]
    assert (note, name) == (jobs.CPENG_DRAFTS_NOTE, "cpeng-04")
    assert "*Draft from [[Dellows Outlet]], [[Site Visit]]: check every fact.*" in text
    assert asyncio.run(jobs.cpeng_drafts(siris)) == {"drafted": []}   # nothing new: not redrafted
