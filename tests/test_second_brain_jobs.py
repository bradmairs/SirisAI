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
        self.filed: list[str] = []
        self.relinked = 0
        self.sources: dict[str, str] = {}
        self.types: dict[str, str] = {}

    def inbox(self) -> dict:
        return {"memory": list(self.memory), "notes": list(self.inbox_notes)}

    def route(self, title: str, content: str, type: str = "auto") -> dict:
        for word, note in self.known.items():
            if content.startswith(word):
                return {"action": "update", "title": note}
        return {"action": "update", "title": jobs.MEMORY_INBOX_TITLE}

    def clear_memory_inbox(self, filed: list[dict]) -> dict:
        self.cleared += filed
        return {"archived": len(filed)}

    def file_note(self, title: str, type: str = "auto") -> dict:
        self.filed.append(title)
        if title == "Dom":
            return {"action": "merged", "title": title, "into": "Dom Mairs"}
        return {"action": "left" if title == "Hmm" else "filed", "title": title}

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
    siris = _siris(brain_dream_hour_local=None, brain_weekly_hour_local=None, brain_upkeep_hour_local=None)
    siris.second_brain = FakeBrain()
    siris.tools.register(FakeCalendar([]))
    assert asyncio.run(jobs.run_due(siris, _at(5))) == {}          # before 06:00
    assert "meetings" in asyncio.run(jobs.run_due(siris, _at(6)))
    assert asyncio.run(jobs.run_due(siris, _at(9))) == {}          # already ran today
    assert "meetings" in asyncio.run(jobs.run_due(siris, _at(6, date(2026, 10, 7))))


def test_weekly_only_on_its_day() -> None:
    siris = _siris(brain_meeting_notes_hour_local=None, brain_dream_hour_local=None, brain_upkeep_hour_local=None,
                   brain_weekly_day=1)
    siris.second_brain = FakeBrain()
    assert {"weekly", "review"} <= set(asyncio.run(jobs.run_due(siris, _at(18))))   # Tuesday = 1
    siris.state.set(jobs.STATE_KEY, {})
    assert asyncio.run(jobs.run_due(siris, _at(18, date(2026, 10, 7)))) == {}


def test_a_failing_job_is_reported_and_not_retried_today() -> None:
    siris = _siris(brain_dream_hour_local=None, brain_weekly_hour_local=None, brain_upkeep_hour_local=None)
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


# ---------- upkeep ----------


def _entry(i: str, text: str) -> dict:
    return {"id": i, "date": "2026-10-05", "text": text, "plain": text}


def test_upkeep_runs_nightly_at_its_hour() -> None:
    siris = _siris(brain_meeting_notes_hour_local=None, brain_dream_hour_local=None, brain_weekly_hour_local=None)
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


def test_inbox_notes_are_filed_by_the_router() -> None:
    siris = _siris()
    brain = siris.second_brain = FakeBrain(inbox_notes=[{"title": t} for t in ("Knee Soreness", "Dom", "Hmm")])
    assert jobs.file_inbox_notes(siris) == {"filed": ["Knee Soreness"], "merged": ["Dom Mairs"], "left": ["Hmm"]}
    assert brain.filed == ["Knee Soreness", "Dom", "Hmm"]


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


def test_a_quiet_upkeep_sends_nothing() -> None:
    siris = _siris()
    siris.second_brain = FakeBrain()
    siris._notify_client = FakeNotify()
    asyncio.run(jobs.upkeep(siris, _at(2)))
    assert siris._notify_client.sent == []


def test_weekly_review_note() -> None:
    siris = _siris()
    brain = siris.second_brain = FakeBrain()
    siris.llm = FakeLLM("## This week\n- Upgraded the server.")
    assert asyncio.run(jobs.weekly_review(siris, DAY)) == {"written": "2026-W41-review"}
    item = brain.learned[0]
    assert item["folder"] == "Daily" and item["tags"] == ["weekly-review"] and "Upgraded" in item["content"]
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
