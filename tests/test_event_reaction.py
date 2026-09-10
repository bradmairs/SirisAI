from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import Settings
from app.core.siris import SirisAI
from app.db.base import Base
from app.memory.events import EventMemoryStore
from app.skills.builtin.event_reaction import EventReactionSkill


# EventReactionCursor is a single global row, same "not user-scoped, so
# each test gets its own isolated in-memory engine rather than the
# shared test-session DB" reasoning test_notify.py's PendingNotificationStore
# tests already use -- get_reaction_cursor()'s default would otherwise
# depend on whatever other tests already advanced it to.
def _isolated_events_store() -> EventMemoryStore:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    return EventMemoryStore(sessionmaker(bind=engine))


class _FakeEventsStore:
    def __init__(self, cursor: int, events: list[dict[str, Any]]) -> None:
        self._cursor = cursor
        self._events = events
        self.advance_calls: list[int] = []

    def get_reaction_cursor(self) -> int:
        return self._cursor

    def events_since(self, last_id: int, limit: int = 100) -> list[dict[str, Any]]:
        assert last_id == self._cursor
        return [e for e in self._events if e["id"] > last_id][:limit]

    def advance_reaction_cursor(self, event_id: int) -> None:
        self.advance_calls.append(event_id)
        self._cursor = event_id


class _FakeSirisForEventReaction:
    def __init__(self, cursor: int, events: list[dict[str, Any]]) -> None:
        self.events = _FakeEventsStore(cursor, events)


def _event(
    id: int, description: str, source: str | None = None, entity_name: str | None = None
) -> dict[str, Any]:
    return {"id": id, "description": description, "source": source, "entity_name": entity_name}


# -- EventReactionSkill


def test_no_new_events_reports_nothing_and_does_not_advance_the_cursor() -> None:
    siris = _FakeSirisForEventReaction(cursor=5, events=[])

    result = EventReactionSkill().run(siris)

    assert result == {"checked": True, "flagged": [], "warnings": []}
    assert siris.events.advance_calls == []


def test_new_events_with_no_keyword_hits_are_not_flagged_but_cursor_advances() -> None:
    siris = _FakeSirisForEventReaction(
        cursor=0,
        events=[_event(1, "movie added"), _event(2, "backup completed")],
    )

    result = EventReactionSkill().run(siris)

    assert result == {"checked": True, "flagged": [], "warnings": []}
    assert siris.events.advance_calls == [2]


def test_a_keyword_match_is_flagged_with_source() -> None:
    siris = _FakeSirisForEventReaction(
        cursor=0, events=[_event(1, "NAS went offline", source="nas")]
    )

    result = EventReactionSkill().run(siris)

    assert result == {
        "checked": True,
        "flagged": [1],
        "warnings": ["Event flagged: NAS went offline (source: nas)"],
    }
    assert siris.events.advance_calls == [1]


def test_a_keyword_match_without_a_source_omits_the_source_suffix() -> None:
    siris = _FakeSirisForEventReaction(cursor=0, events=[_event(1, "sync failed")])

    result = EventReactionSkill().run(siris)

    assert result["warnings"] == ["Event flagged: sync failed"]


def test_a_keyword_match_with_an_entity_name_appends_it_last() -> None:
    """Cross-domain correlation (a Jarvis-concept brainstorm item):
    NotificationManager's _EVENT_ENTITY_PATTERN anchors on "(entity: X)"
    at the end of the message, so entity comes after source."""
    siris = _FakeSirisForEventReaction(
        cursor=0,
        events=[_event(1, "NAS went offline", source="nas", entity_name="NAS")],
    )

    result = EventReactionSkill().run(siris)

    assert result["warnings"] == ["Event flagged: NAS went offline (source: nas) (entity: NAS)"]


def test_a_keyword_match_without_an_entity_name_omits_the_entity_suffix() -> None:
    siris = _FakeSirisForEventReaction(cursor=0, events=[_event(1, "sync failed")])

    result = EventReactionSkill().run(siris)

    assert result["warnings"] == ["Event flagged: sync failed"]


def test_keyword_matching_is_case_insensitive() -> None:
    siris = _FakeSirisForEventReaction(cursor=0, events=[_event(1, "Disk is CRITICAL")])

    result = EventReactionSkill().run(siris)

    assert result["flagged"] == [1]


def test_only_new_events_since_the_cursor_are_considered() -> None:
    siris = _FakeSirisForEventReaction(
        cursor=1,
        events=[_event(1, "old error"), _event(2, "connection error")],
    )

    result = EventReactionSkill().run(siris)

    assert result["flagged"] == [2]
    assert siris.events.advance_calls == [2]


def test_cursor_advances_to_the_highest_new_event_id_even_when_nothing_is_flagged() -> (
    None
):
    siris = _FakeSirisForEventReaction(
        cursor=0, events=[_event(1, "fine"), _event(2, "also fine"), _event(3, "ok")]
    )

    EventReactionSkill().run(siris)

    assert siris.events.advance_calls == [3]


# -- Registration (always registered, same as new_device_alert)


def test_event_reaction_check_is_registered_as_a_skill() -> None:
    siris = SirisAI(settings=Settings())
    assert siris.tools.get("event_reaction_check") is not None
    assert "event_reaction_check" in siris.capabilities()["skills"]


# -- EventMemoryStore's cursor methods, against an isolated in-memory DB


def test_reaction_cursor_defaults_to_zero_before_any_row_exists() -> None:
    events = _isolated_events_store()
    assert events.get_reaction_cursor() == 0


def test_advance_reaction_cursor_persists_and_can_move_forward_again() -> None:
    events = _isolated_events_store()

    events.advance_reaction_cursor(3)
    assert events.get_reaction_cursor() == 3

    events.advance_reaction_cursor(9)
    assert events.get_reaction_cursor() == 9


def test_events_since_returns_only_newer_events_oldest_first() -> None:
    events = _isolated_events_store()
    first = events.log_event("one")
    second = events.log_event("two")
    third = events.log_event("three")

    newer = events.events_since(first["id"])

    assert [e["id"] for e in newer] == [second["id"], third["id"]]


def test_events_since_respects_the_limit() -> None:
    events = _isolated_events_store()
    events.log_event("one")
    events.log_event("two")
    events.log_event("three")

    newer = events.events_since(0, limit=2)

    assert [e["description"] for e in newer] == ["one", "two"]


# -- End-to-end via the real skill against an isolated store


class _SirisWithIsolatedEvents:
    def __init__(self) -> None:
        self.events = _isolated_events_store()


def test_end_to_end_flags_a_real_logged_event_exactly_once() -> None:
    siris = _SirisWithIsolatedEvents()
    siris.events.log_event("disk offline", source="nas")

    first_run = EventReactionSkill().run(siris)
    assert first_run["warnings"] == ["Event flagged: disk offline (source: nas)"]

    second_run = EventReactionSkill().run(siris)
    assert second_run == {"checked": True, "flagged": [], "warnings": []}
