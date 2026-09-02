from fastapi.testclient import TestClient

from app.core.siris import SirisAI, get_siris
from app.main import app

client = TestClient(app)


def test_health_ok() -> None:
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["database"] == "ok"


def test_siris_status() -> None:
    response = client.get("/siris/status")
    assert response.status_code == 200
    body = response.json()
    assert body["app_name"] == "SirisAI"
    assert "echo" in body["tools"]


def test_chat_without_llm_backend_returns_502() -> None:
    # Default test config has llm_provider="none" -> NullLLMClient.
    response = client.post("/siris/chat", json={"prompt": "hello"})
    assert response.status_code == 502


class _FakeLLMClient:
    async def generate(self, prompt: str, **kwargs: object) -> str:
        return f"echo: {prompt}"

    async def aclose(self) -> None:
        pass


def test_chat_with_backend_configured() -> None:
    fake_siris = get_siris()
    fake_siris.llm = _FakeLLMClient()  # type: ignore[assignment]
    app.dependency_overrides[get_siris] = lambda: fake_siris
    try:
        response = client.post("/siris/chat", json={"prompt": "hello"})
        assert response.status_code == 200
        assert response.json() == {"response": "echo: hello"}
    finally:
        app.dependency_overrides.pop(get_siris, None)
