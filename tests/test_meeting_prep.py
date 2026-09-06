from typing import Any

from app.core.config import Settings
from app.core.siris import SirisAI
from app.skills.builtin.meeting_prep import MeetingPrepSkill


class _FakeTools:
    def __init__(self, has_calendar: bool = True, has_obsidian: bool = False) -> None:
        self._present = set()
        if has_calendar:
            self._present.add("calendar_check_conflicts")
        if has_obsidian:
            self._present.add("obsidian_search")

    def get(self, name: str) -> object | None:
        return object() if name in self._present else None


class _FakeMemory:
    def __init__(
        self, memories_by_query: dict[str, list[dict[str, Any]]] | None = None
    ) -> None:
        self._memories_by_query = memories_by_query or {}
        self.recall_calls: list[str] = []

    def recall(
        self, query: str | None = None, limit: int = 20, **kwargs: Any
    ) -> list[dict[str, Any]]:
        self.recall_calls.append(query or "")
        return self._memories_by_query.get(query or "", [])


class _FakeSirisForMeetingPrep:
    def __init__(
        self,
        has_calendar: bool = True,
        has_obsidian: bool = False,
        events: list[dict[str, Any]] | None = None,
        notes_by_query: dict[str, list[dict[str, Any]]] | None = None,
        memories_by_query: dict[str, list[dict[str, Any]]] | None = None,
        obsidian_raises: bool = False,
    ) -> None:
        self.tools = _FakeTools(has_calendar, has_obsidian)
        self.memory = _FakeMemory(memories_by_query)
        self._events = events if events is not None else []
        self._notes_by_query = notes_by_query or {}
        self._obsidian_raises = obsidian_raises
        self.run_tool_calls: list[tuple[str, dict[str, Any]]] = []

    def run_tool(self, name: str, arguments: dict[str, Any], **kwargs: Any) -> Any:
        self.run_tool_calls.append((name, arguments))
        if name == "calendar_check_conflicts":
            return self._events
        if name == "obsidian_search":
            if self._obsidian_raises:
                raise ConnectionError("obsidian vault unreachable")
            return self._notes_by_query.get(arguments["query"], [])
        raise AssertionError(f"unexpected tool call: {name}")


_STANDUP_EVENT = {
    "summary": "Standup",
    "start": "2026-01-10T10:00:00+00:00",
    "end": "2026-01-10T10:15:00+00:00",
    "location": None,
    "calendar": "Work",
}


def test_not_configured_without_calendar() -> None:
    siris = _FakeSirisForMeetingPrep(has_calendar=False)

    result = MeetingPrepSkill().run(siris)

    assert result == {"checked": False, "reason": "Calendar is not configured."}
    assert siris.run_tool_calls == []


def test_no_upcoming_events_returns_empty_meetings() -> None:
    siris = _FakeSirisForMeetingPrep(events=[])

    result = MeetingPrepSkill().run(siris)

    assert result == {"checked": True, "meetings": []}


def test_event_with_no_related_notes_or_memories() -> None:
    siris = _FakeSirisForMeetingPrep(events=[_STANDUP_EVENT])

    result = MeetingPrepSkill().run(siris)

    assert result["meetings"] == [
        {"event": _STANDUP_EVENT, "related_notes": [], "related_memories": []}
    ]
    assert siris.memory.recall_calls == ["Standup"]


def test_related_memories_are_surfaced_by_content() -> None:
    memories = [{"id": 1, "content": "Standup happens daily at 10am"}]
    siris = _FakeSirisForMeetingPrep(
        events=[_STANDUP_EVENT], memories_by_query={"Standup": memories}
    )

    result = MeetingPrepSkill().run(siris)

    assert result["meetings"][0]["related_memories"] == [
        "Standup happens daily at 10am"
    ]


def test_related_notes_are_surfaced_when_obsidian_configured() -> None:
    notes = [{"path": "Projects/Standup.md", "excerpts": ["..."]}]
    siris = _FakeSirisForMeetingPrep(
        has_obsidian=True, events=[_STANDUP_EVENT], notes_by_query={"Standup": notes}
    )

    result = MeetingPrepSkill().run(siris)

    assert result["meetings"][0]["related_notes"] == notes


def test_related_notes_omitted_without_obsidian_configured() -> None:
    siris = _FakeSirisForMeetingPrep(has_obsidian=False, events=[_STANDUP_EVENT])

    result = MeetingPrepSkill().run(siris)

    assert result["meetings"][0]["related_notes"] == []
    assert not any(name == "obsidian_search" for name, _ in siris.run_tool_calls)


def test_survives_obsidian_being_unreachable() -> None:
    siris = _FakeSirisForMeetingPrep(
        has_obsidian=True, events=[_STANDUP_EVENT], obsidian_raises=True
    )

    result = MeetingPrepSkill().run(siris)

    assert result["meetings"][0]["related_notes"] == []


def test_event_with_no_summary_skips_search_entirely() -> None:
    event = {**_STANDUP_EVENT, "summary": ""}
    siris = _FakeSirisForMeetingPrep(has_obsidian=True, events=[event])

    result = MeetingPrepSkill().run(siris)

    assert result["meetings"][0]["related_notes"] == []
    assert result["meetings"][0]["related_memories"] == []
    assert siris.memory.recall_calls == []
    assert not any(name == "obsidian_search" for name, _ in siris.run_tool_calls)


def test_meeting_prep_tool_is_registered_as_a_skill() -> None:
    siris = SirisAI(settings=Settings())
    assert siris.tools.get("meeting_prep") is not None
    assert "meeting_prep" in siris.capabilities()["skills"]
