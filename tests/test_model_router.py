from typing import Any

from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.siris import SirisAI, get_siris
from app.llm.base import LLMResponse
from app.llm.router import ModelRouter
from app.main import app


class _FakeLLMClient:
    def __init__(self, label: str) -> None:
        self.label = label
        self.closed = False
        self.calls: list[str] = []

    async def generate(self, messages: list[dict[str, Any]], *, tools: Any = None, **kwargs: object) -> LLMResponse:
        self.calls.append(messages[-1]["content"])
        return LLMResponse(text=f"{self.label} reply")

    async def aclose(self) -> None:
        self.closed = True


def test_pick_defaults_to_general_with_policy_off() -> None:
    general, fast = _FakeLLMClient("general"), _FakeLLMClient("fast")
    router = ModelRouter(fast=fast, policy="off", fast_max_prompt_chars=120)
    role, client = router.pick(general, "hi")
    assert role == "general"
    assert client is general


def test_pick_honors_explicit_fast_role_even_with_policy_off() -> None:
    general, fast = _FakeLLMClient("general"), _FakeLLMClient("fast")
    router = ModelRouter(fast=fast, policy="off", fast_max_prompt_chars=120)
    role, client = router.pick(general, "hi", role="fast")
    assert role == "fast"
    assert client is fast


def test_pick_falls_back_to_general_when_fast_role_requested_but_not_configured() -> None:
    general = _FakeLLMClient("general")
    router = ModelRouter(fast=None, policy="off", fast_max_prompt_chars=120)
    role, client = router.pick(general, "hi", role="fast")
    assert role == "general"
    assert client is general


def test_prompt_length_policy_routes_short_prompts_to_fast() -> None:
    general, fast = _FakeLLMClient("general"), _FakeLLMClient("fast")
    router = ModelRouter(fast=fast, policy="prompt_length", fast_max_prompt_chars=10)
    role, client = router.pick(general, "short", role=None)
    assert role == "fast"
    assert client is fast


def test_prompt_length_policy_routes_long_prompts_to_general() -> None:
    general, fast = _FakeLLMClient("general"), _FakeLLMClient("fast")
    router = ModelRouter(fast=fast, policy="prompt_length", fast_max_prompt_chars=10)
    role, client = router.pick(general, "this prompt is much longer than the threshold", role=None)
    assert role == "general"
    assert client is general


def test_available_roles_reflects_whether_fast_is_configured() -> None:
    assert ModelRouter(fast=None, policy="off", fast_max_prompt_chars=120).available_roles() == ["general"]
    fast = _FakeLLMClient("fast")
    assert ModelRouter(fast=fast, policy="off", fast_max_prompt_chars=120).available_roles() == ["general", "fast"]


def test_aclose_only_closes_fast_client() -> None:
    import asyncio

    fast = _FakeLLMClient("fast")
    router = ModelRouter(fast=fast, policy="off", fast_max_prompt_chars=120)
    asyncio.run(router.aclose())
    assert fast.closed is True


def test_chat_uses_fast_model_when_role_requested(client: TestClient) -> None:
    general, fast = _FakeLLMClient("general"), _FakeLLMClient("fast")
    fake_siris = SirisAI(
        settings=Settings(),
        llm=general,
        model_router=ModelRouter(fast=fast, policy="off", fast_max_prompt_chars=120),
    )
    app.dependency_overrides[get_siris] = lambda: fake_siris
    try:
        response = client.post(
            "/siris/chat", json={"prompt": "hi", "model_role": "fast", "use_context": False, "use_tools": False}
        )
        assert response.status_code == 200
        assert response.json()["model_role"] == "fast"
        assert response.json()["response"] == "fast reply"
        assert fast.calls == ["hi"]
        assert general.calls == []
    finally:
        app.dependency_overrides.pop(get_siris, None)


def test_chat_prompt_length_policy_end_to_end(client: TestClient) -> None:
    general, fast = _FakeLLMClient("general"), _FakeLLMClient("fast")
    fake_siris = SirisAI(
        settings=Settings(),
        llm=general,
        model_router=ModelRouter(fast=fast, policy="prompt_length", fast_max_prompt_chars=5),
    )
    app.dependency_overrides[get_siris] = lambda: fake_siris
    try:
        short = client.post("/siris/chat", json={"prompt": "hi", "use_context": False, "use_tools": False})
        assert short.json()["model_role"] == "fast"

        long = client.post(
            "/siris/chat", json={"prompt": "a much longer prompt here", "use_context": False, "use_tools": False}
        )
        assert long.json()["model_role"] == "general"
    finally:
        app.dependency_overrides.pop(get_siris, None)


def test_status_reports_model_router_info(client: TestClient) -> None:
    response = client.get("/siris/status")
    body = response.json()
    assert body["model_router_policy"] == "off"
    assert body["model_router_roles"] == ["general"]


def test_fast_llm_configured_adds_fast_role() -> None:
    siris = SirisAI(settings=Settings(fast_llm_provider="ollama", fast_llm_ollama_model="llama3.2:1b"))
    try:
        assert siris.available_model_roles() == ["general", "fast"]
    finally:
        import asyncio

        asyncio.run(siris.model_router.aclose())  # type: ignore[union-attr]
