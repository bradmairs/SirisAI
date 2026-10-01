import uuid
from typing import Any

from fastapi.testclient import TestClient

from app.core.siris import SirisAI, get_siris
from app.llm.base import LLMResponse
from app.main import app


def test_build_includes_system_status_events_and_memories() -> None:
    siris = SirisAI(settings=get_siris().settings)
    marker = uuid.uuid4().hex  # one long search term; dashed uuid chunks can collide
    siris.events.log_event(f"event {marker}")
    siris.memory.remember(f"memory {marker}")

    text = siris.context.build(siris, marker)
    assert text is not None
    assert "Current system status" in text
    assert f"event {marker}" in text
    assert f"memory {marker}" in text


def test_build_finds_a_memory_from_a_natural_language_question_not_a_literal_substring() -> (
    None
):
    """The exact bug this fix exists for: a real user asks "what's my
    name?" -- a question, not the stored fact's own wording -- and the
    old all-or-nothing substring match (the *entire* question had to
    appear verbatim inside the memory) would never surface "User's name
    is Brad-<marker>", since the question is nowhere close to being a
    literal substring of that sentence. SirisAI would then truthfully (but
    wrongly, from the user's perspective) say it couldn't recall
    something it had, in fact, already been told."""
    siris = SirisAI(settings=get_siris().settings)
    marker = uuid.uuid4().hex  # one long search term; dashed uuid chunks can collide
    siris.memory.remember(f"User's name is Brad-{marker}")

    text = siris.context.build(siris, "What is my name?")
    assert text is not None
    assert f"Brad-{marker}" in text


def test_build_always_includes_system_status_when_tool_present() -> None:
    siris = SirisAI(settings=get_siris().settings)
    text = siris.context.build(siris, "totally unrelated query with no matches")
    assert text is not None
    assert "Current system status" in text


def test_context_does_not_hit_audit_log() -> None:
    siris = SirisAI(settings=get_siris().settings)
    before = len(siris.audit.list_recent(limit=1000))
    siris.context.build(siris, "some prompt")
    after = len(siris.audit.list_recent(limit=1000))
    assert after == before


class _RecordingLLMClient:
    def __init__(self) -> None:
        self.calls: list[list[dict[str, Any]]] = []

    async def generate(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        **kwargs: object,
    ) -> LLMResponse:
        self.calls.append(messages)
        return LLMResponse(text="ok")

    async def aclose(self) -> None:
        pass


def test_chat_prepends_context_as_system_message_by_default(client: TestClient) -> None:
    fake_llm = _RecordingLLMClient()
    fake_siris = get_siris()
    fake_siris.llm = fake_llm  # type: ignore[assignment]
    app.dependency_overrides[get_siris] = lambda: fake_siris
    try:
        client.post("/siris/chat", json={"prompt": "how's everything looking?"})
    finally:
        app.dependency_overrides.pop(get_siris, None)

    sent_messages = fake_llm.calls[0]
    assert sent_messages[0]["role"] == "system"
    assert "Current system status" in sent_messages[0]["content"]
    assert sent_messages[-1] == {"role": "user", "content": "how's everything looking?"}


def test_chat_skips_context_when_use_context_false(client: TestClient) -> None:
    fake_llm = _RecordingLLMClient()
    fake_siris = get_siris()
    fake_siris.llm = fake_llm  # type: ignore[assignment]
    app.dependency_overrides[get_siris] = lambda: fake_siris
    try:
        client.post(
            "/siris/chat", json={"prompt": "no context please", "use_context": False}
        )
    finally:
        app.dependency_overrides.pop(get_siris, None)

    sent_messages = fake_llm.calls[0]
    assert all(m["role"] != "system" for m in sent_messages)
