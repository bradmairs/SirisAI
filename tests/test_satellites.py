"""Room/presence-aware voice routing (milestone 48): SatelliteStore's
registry + "current room" logic, the registration endpoints, and
speak_in_current_room actually pushing a reply somewhere other than the
device a conversation started on. See satellite/tests/ for the
satellite-side (client registration, the inbound listener) coverage.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import Settings
from app.core.siris import SirisAI, get_siris
from app.db.base import Base
from app.main import app
from app.plugins.base import ToolPermission
from app.plugins.builtin.satellites import SpeakInCurrentRoomTool
from app.satellites.service import SatelliteStore


def _isolated_satellites() -> SatelliteStore:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    return SatelliteStore(sessionmaker(bind=engine))


def _room(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4()}"


# -- SatelliteStore


def test_register_creates_a_new_satellite() -> None:
    store = _isolated_satellites()
    room = _room("kitchen")

    result = store.register(room, "http://192.168.1.50:8090")

    assert result["room"] == room
    assert result["callback_url"] == "http://192.168.1.50:8090"
    assert store.get_by_room(room) is not None


def test_register_upserts_by_room_rather_than_duplicating() -> None:
    store = _isolated_satellites()
    room = _room("office")
    store.register(room, "http://192.168.1.50:8090")
    store.register(
        room, "http://192.168.1.51:9090"
    )  # same room, new address (restarted process)

    matching = [s for s in store.list_satellites() if s["room"] == room]
    assert len(matching) == 1
    assert matching[0]["callback_url"] == "http://192.168.1.51:9090"


def test_get_by_room_returns_none_for_an_unregistered_room() -> None:
    assert _isolated_satellites().get_by_room(_room("nowhere")) is None


def test_record_activity_bumps_last_active_at_for_a_registered_room() -> None:
    store = _isolated_satellites()
    room = _room("kitchen")
    original = store.register(room, "http://192.168.1.50:8090")

    store.record_activity(room)

    assert store.get_by_room(room)["last_active_at"] >= original["last_active_at"]


def test_record_activity_is_a_no_op_for_an_unregistered_room() -> None:
    store = _isolated_satellites()
    store.record_activity(_room("nowhere"))  # must not raise
    assert store.list_satellites() == []


def test_current_room_is_none_with_no_satellites() -> None:
    assert _isolated_satellites().current_room() is None


def test_current_room_is_whichever_room_was_most_recently_active() -> None:
    store = _isolated_satellites()
    kitchen = _room("kitchen")
    office = _room("office")
    store.register(kitchen, "http://kitchen:8090")
    store.register(office, "http://office:8090")

    assert store.current_room() == office  # registered most recently

    store.record_activity(kitchen)
    assert store.current_room() == kitchen  # now the more recently active one


# -- Conversation handoff between rooms (milestone 75)


def test_record_activity_stores_the_conversation_id_for_handoff() -> None:
    store = _isolated_satellites()
    kitchen = _room("kitchen")
    bedroom = _room("bedroom")
    store.register(kitchen, "http://kitchen:8090")
    store.register(bedroom, "http://bedroom:8090")

    store.record_activity(kitchen, conversation_id="convo-1")

    assert (
        store.handoff_candidate(exclude_room=bedroom, within_seconds=120) == "convo-1"
    )


def test_record_activity_without_a_conversation_id_leaves_the_stored_one_alone() -> (
    None
):
    store = _isolated_satellites()
    kitchen = _room("kitchen")
    bedroom = _room("bedroom")
    store.register(kitchen, "http://kitchen:8090")
    store.register(bedroom, "http://bedroom:8090")
    store.record_activity(kitchen, conversation_id="convo-1")

    store.record_activity(
        kitchen
    )  # e.g. a non-satellite-aware caller, bumps activity only

    assert (
        store.handoff_candidate(exclude_room=bedroom, within_seconds=120) == "convo-1"
    )


def test_handoff_candidate_is_none_with_no_other_rooms() -> None:
    store = _isolated_satellites()
    room = _room("kitchen")
    store.register(room, "http://kitchen:8090")
    store.record_activity(room, conversation_id="convo-1")

    assert store.handoff_candidate(exclude_room=room, within_seconds=120) is None


def test_handoff_candidate_returns_the_other_rooms_recent_conversation() -> None:
    store = _isolated_satellites()
    kitchen = _room("kitchen")
    bedroom = _room("bedroom")
    store.register(kitchen, "http://kitchen:8090")
    store.register(bedroom, "http://bedroom:8090")
    store.record_activity(kitchen, conversation_id="convo-1")

    assert (
        store.handoff_candidate(exclude_room=bedroom, within_seconds=120) == "convo-1"
    )


def test_handoff_candidate_ignores_activity_older_than_the_window() -> None:
    store = _isolated_satellites()
    kitchen = _room("kitchen")
    bedroom = _room("bedroom")
    store.register(kitchen, "http://kitchen:8090")
    store.register(bedroom, "http://bedroom:8090")
    store.record_activity(
        kitchen,
        conversation_id="convo-1",
        at=datetime.now(UTC) - timedelta(seconds=300),
    )

    assert store.handoff_candidate(exclude_room=bedroom, within_seconds=120) is None


def test_handoff_candidate_ignores_a_room_with_no_recorded_conversation() -> None:
    store = _isolated_satellites()
    kitchen = _room("kitchen")
    bedroom = _room("bedroom")
    store.register(
        kitchen, "http://kitchen:8090"
    )  # activity, but never a conversation_id
    store.register(bedroom, "http://bedroom:8090")

    assert store.handoff_candidate(exclude_room=bedroom, within_seconds=120) is None


def test_handoff_candidate_is_none_with_no_satellites_at_all() -> None:
    store = _isolated_satellites()
    assert (
        store.handoff_candidate(exclude_room=_room("nowhere"), within_seconds=120)
        is None
    )


# -- GET/POST /siris/satellites


def test_register_and_list_satellites_via_endpoints(client: TestClient) -> None:
    room = _room("living-room")
    response = client.post(
        "/siris/satellites/register",
        json={"room": room, "callback_url": "http://192.168.1.60:8090"},
    )
    assert response.status_code == 200
    assert response.json()["room"] == room

    listed = client.get("/siris/satellites").json()
    assert any(
        s["room"] == room and s["callback_url"] == "http://192.168.1.60:8090"
        for s in listed
    )


# -- SpeakInCurrentRoomTool


def test_speak_in_current_room_reports_no_satellite_known_when_none_registered() -> (
    None
):
    tool = SpeakInCurrentRoomTool(
        _isolated_satellites(), "http://tts", "tts-model", "voice"
    )
    result = tool.run(message="hello")
    assert result == {"sent": False, "reason": "no satellite currently known"}


def test_speak_in_current_room_synthesizes_and_pushes_to_the_current_rooms_satellite() -> (
    None
):
    store = _isolated_satellites()
    room = _room("kitchen")
    store.register(room, "http://kitchen.local:8090")

    tts_calls: list[dict[str, Any]] = []
    push_calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "tts.local":
            import json

            tts_calls.append(json.loads(request.content))
            return httpx.Response(200, content=b"fake-mp3-bytes")
        if request.url.host == "kitchen.local":
            push_calls.append(request)
            return httpx.Response(200)
        raise AssertionError(f"unexpected request to {request.url}")

    tool = SpeakInCurrentRoomTool(
        store,
        "http://tts.local",
        "tts-model",
        "nova",
        transport=httpx.MockTransport(handler),
    )

    result = tool.run(message="the dryer finished")

    assert result == {"sent": True, "room": room}
    assert tts_calls == [
        {
            "model": "tts-model",
            "voice": "nova",
            "input": "the dryer finished",
            "response_format": "mp3",
        }
    ]
    assert len(push_calls) == 1
    assert push_calls[0].url.path == "/play"
    assert push_calls[0].content == b"fake-mp3-bytes"


def test_speak_in_current_room_pronounces_its_own_name_as_cyrus() -> None:
    store = _isolated_satellites()
    room = _room("kitchen")
    store.register(room, "http://kitchen.local:8090")

    tts_calls: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "tts.local":
            import json

            tts_calls.append(json.loads(request.content))
            return httpx.Response(200, content=b"fake-mp3-bytes")
        return httpx.Response(200)

    tool = SpeakInCurrentRoomTool(store, "http://tts.local", "tts-model", "nova", transport=httpx.MockTransport(handler))
    tool.run(message="SirisAI noticed the dryer finished")

    assert tts_calls[0]["input"] == "Cyrus AI noticed the dryer finished"


def test_speak_in_current_room_propagates_a_push_failure() -> None:
    store = _isolated_satellites()
    room = _room("kitchen")
    store.register(room, "http://kitchen.local:8090")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "tts.local":
            return httpx.Response(200, content=b"fake-mp3-bytes")
        return httpx.Response(503)

    tool = SpeakInCurrentRoomTool(
        store,
        "http://tts.local",
        "tts-model",
        "nova",
        transport=httpx.MockTransport(handler),
    )

    try:
        tool.run(message="hi")
        raised = False
    except httpx.HTTPStatusError:
        raised = True
    assert raised


# -- Registration gating (tts_provider != "none")


def test_speak_in_current_room_registered_when_tts_configured() -> None:
    siris = SirisAI(
        settings=Settings(
            tts_provider="openai_compatible", tts_base_url="http://tts.local"
        )
    )
    tool = siris.tools.get("speak_in_current_room")
    assert tool is not None
    assert tool.permission is ToolPermission.SAFE_ACTION


def test_speak_in_current_room_not_registered_when_tts_not_configured() -> None:
    siris = SirisAI(settings=Settings(tts_provider="none"))
    assert siris.tools.get("speak_in_current_room") is None


# -- POST /siris/chat with room: end-to-end room-activity recording


def test_chat_with_room_updates_current_room(client: TestClient) -> None:
    from app.llm.base import LLMResponse

    room = _room("bedroom")
    client.post(
        "/siris/satellites/register",
        json={"room": room, "callback_url": "http://bedroom:8090"},
    )

    class _FixedLLMClient:
        async def generate(
            self, messages: Any, *, tools: Any = None, **kwargs: object
        ) -> LLMResponse:
            return LLMResponse(text="ok")

        async def aclose(self) -> None:
            pass

    fake_siris = get_siris()
    original_llm = fake_siris.llm
    fake_siris.llm = _FixedLLMClient()  # type: ignore[assignment]
    try:
        response = client.post(
            "/siris/chat", json={"prompt": "hello", "use_context": False, "room": room}
        )
        assert response.status_code == 200
    finally:
        fake_siris.llm = original_llm

    assert fake_siris.satellites.current_room() == room


def test_chat_with_room_hands_off_conversation_from_a_recently_active_room(
    client: TestClient,
) -> None:
    """The actual gap milestone 75 closes: each satellite process keeps
    its own conversation_id in memory (satellite/siris_satellite/client.py),
    so the first request from a different room always arrives with none
    -- without a handoff, that silently starts a brand-new conversation
    and loses whatever was just discussed."""
    from app.llm.base import LLMResponse

    kitchen = _room("kitchen")
    bedroom = _room("bedroom")
    client.post(
        "/siris/satellites/register",
        json={"room": kitchen, "callback_url": "http://kitchen:8090"},
    )
    client.post(
        "/siris/satellites/register",
        json={"room": bedroom, "callback_url": "http://bedroom:8090"},
    )

    class _FixedLLMClient:
        async def generate(
            self, messages: Any, *, tools: Any = None, **kwargs: object
        ) -> LLMResponse:
            return LLMResponse(text="ok")

        async def aclose(self) -> None:
            pass

    fake_siris = get_siris()
    original_llm = fake_siris.llm
    fake_siris.llm = _FixedLLMClient()  # type: ignore[assignment]
    try:
        first = client.post(
            "/siris/chat",
            json={
                "prompt": "hello from the kitchen",
                "use_context": False,
                "room": kitchen,
            },
        )
        first_conversation_id = first.json()["conversation_id"]

        second = client.post(
            "/siris/chat",
            json={"prompt": "still here", "use_context": False, "room": bedroom},
        )
        assert second.json()["conversation_id"] == first_conversation_id
    finally:
        fake_siris.llm = original_llm


# Note: the staleness cutoff itself (an old room's activity being
# correctly ignored) is covered above by
# test_handoff_candidate_ignores_activity_older_than_the_window, against
# an isolated store -- not repeated here end-to-end. This file's `client`
# fixture is session-scoped, sharing one satellite registry across every
# test in it, so an unrelated, still-fresh room from an *earlier* test
# can legitimately win the "most recently active" slot and mask what a
# deliberately-backdated room does, making that specific case unreliable
# to assert against the real route.
