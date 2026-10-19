import struct
from typing import Any

from fastapi.testclient import TestClient

from app.core.siris import get_siris
from app.llm.base import LLMResponse, ToolCall
from app.main import app
from tests.test_permissions import _ConfirmRequiredTool


class _EchoLLM:
    async def generate(self, messages: list[dict[str, Any]], *, tools: Any = None, **kwargs: object) -> LLMResponse:
        return LLMResponse(text=f"You said: {messages[-1]['content']}")

    async def aclose(self) -> None:
        pass


class _NeedsConfirmLLM:
    async def generate(self, messages: list[dict[str, Any]], *, tools: Any = None, **kwargs: object) -> LLMResponse:
        return LLMResponse(tool_calls=[ToolCall(name="dangerous_action", arguments={"target": "x"})])

    async def aclose(self) -> None:
        pass


def _with_llm(llm: object) -> None:
    fake = get_siris()
    fake.llm = llm  # type: ignore[assignment]
    if fake.tools.get("dangerous_action") is None:
        fake.tools.register(_ConfirmRequiredTool())
    app.dependency_overrides[get_siris] = lambda: fake


def test_ask_accepts_a_raw_text_body_and_answers_in_plain_text(client: TestClient) -> None:
    _with_llm(_EchoLLM())
    try:
        response = client.post("/siris/ask", content="what's the weather")
    finally:
        app.dependency_overrides.pop(get_siris, None)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert response.text == "You said: what's the weather"
    assert response.headers["x-conversation-id"]


def test_ask_accepts_json_and_continues_a_conversation(client: TestClient) -> None:
    _with_llm(_EchoLLM())
    try:
        first = client.post("/siris/ask", json={"prompt": "hello"})
        second = client.post("/siris/ask", json={"prompt": "again", "conversation_id": first.headers["x-conversation-id"]})
    finally:
        app.dependency_overrides.pop(get_siris, None)
    assert second.headers["x-conversation-id"] == first.headers["x-conversation-id"]
    assert second.text == "You said: again"


def test_ask_rejects_an_empty_question(client: TestClient) -> None:
    _with_llm(_EchoLLM())
    try:
        assert client.post("/siris/ask", content="   ").status_code == 422
        assert client.post("/siris/ask", json={"prompt": ""}).status_code == 422
    finally:
        app.dependency_overrides.pop(get_siris, None)


def test_ask_does_not_run_an_action_that_needs_confirmation(client: TestClient) -> None:
    _with_llm(_NeedsConfirmLLM())
    try:
        response = client.post("/siris/ask", content="do the dangerous thing")
    finally:
        app.dependency_overrides.pop(get_siris, None)
    assert response.status_code == 200
    assert "OK before I run dangerous action" in response.text


def test_ask_requires_the_api_key_when_one_is_configured(client: TestClient) -> None:
    from app.core.config import Settings, get_settings

    app.dependency_overrides[get_settings] = lambda: Settings(api_key="secret-key")
    try:
        assert client.post("/siris/ask", content="hi").status_code == 401
    finally:
        app.dependency_overrides.pop(get_settings, None)


def test_manifest_makes_the_page_installable(client: TestClient) -> None:
    response = client.get("/manifest.webmanifest")
    assert response.status_code == 200
    body = response.json()
    assert body["display"] == "standalone" and body["start_url"] == "/" and body["name"] == "SirisAI"
    assert {i["sizes"] for i in body["icons"]} == {"192x192", "512x512"}


def test_icons_are_real_pngs_of_the_declared_sizes(client: TestClient) -> None:
    for path, size in (("/apple-touch-icon.png", 180), ("/icon-192.png", 192), ("/icon-512.png", 512)):
        response = client.get(path)
        assert response.status_code == 200 and response.headers["content-type"] == "image/png"
        assert response.content[:8] == b"\x89PNG\r\n\x1a\n"
        width, height = struct.unpack(">II", response.content[16:24])
        assert (width, height) == (size, size)


def test_page_links_the_manifest_and_touch_icon(client: TestClient) -> None:
    html = client.get("/").text
    assert 'rel="manifest" href="/manifest.webmanifest"' in html
    assert 'rel="apple-touch-icon"' in html and "apple-mobile-web-app-capable" in html


def test_every_page_uses_sirisos_liquid_glass(client: TestClient) -> None:
    """One look across SirisAI and SirisOS: the shared stylesheet carries
    SirisOS's tokens (ADR 106), and every page loads it and its wallpaper."""
    css = client.get("/siris-glass.css")
    assert css.status_code == 200 and css.headers["content-type"].startswith("text/css")
    for token in ("--accent: #5fd3ff", "--brain: #9b72f7", "--glass-fill: rgba(8, 14, 20, 0.62)", ':root[data-theme="light"]', "prefers-reduced-transparency"):
        assert token in css.text
    for page in ("/", "/hud", "/brain"):
        html = client.get(page).text
        assert '<link rel="stylesheet" href="/siris-glass.css"' in html and 'class="wallpaper"' in html
    index = client.get("/").text
    assert 'id="light-theme-toggle"' in index and 'localStorage.getItem("siris-theme")' in index
    assert client.get("/manifest.webmanifest").json()["theme_color"] == "#000000"
