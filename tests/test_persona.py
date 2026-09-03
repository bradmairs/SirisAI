"""Endpoint-level coverage for the persona/voice consistency layer
(milestone 45) -- the "not configured" case (no persona message at all)
is already precisely covered at the Cortex unit level
(tests/test_cortex.py), so this only proves the positive path actually
reaches the model over the real POST /siris/chat route.
"""

from typing import Any

from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.siris import SirisAI, get_siris
from app.llm.base import LLMResponse
from app.main import app


class _RecordingLLMClient:
    def __init__(self) -> None:
        self.calls: list[list[dict[str, Any]]] = []

    async def generate(
        self, messages: list[dict[str, Any]], *, tools: list[dict[str, Any]] | None = None, **kwargs: object
    ) -> LLMResponse:
        self.calls.append(messages)
        return LLMResponse(text="ok")

    async def aclose(self) -> None:
        pass


def test_chat_prepends_persona_when_configured(client: TestClient) -> None:
    fake_llm = _RecordingLLMClient()
    fake_siris = SirisAI(settings=Settings(persona="You are Jarvis: dry wit, always precise."))
    fake_siris.llm = fake_llm  # type: ignore[assignment]
    app.dependency_overrides[get_siris] = lambda: fake_siris
    try:
        response = client.post("/siris/chat", json={"prompt": "hello", "use_context": False})
        assert response.status_code == 200
    finally:
        app.dependency_overrides.pop(get_siris, None)

    sent_messages = fake_llm.calls[0]
    assert sent_messages[0] == {"role": "system", "content": "You are Jarvis: dry wit, always precise."}
    assert sent_messages[-1] == {"role": "user", "content": "hello"}
