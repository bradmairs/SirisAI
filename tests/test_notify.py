import httpx

from app.core.config import Settings
from app.core.siris import SirisAI
from app.notify.client import NotifyClient


def _mock_client(handler) -> NotifyClient:
    return NotifyClient(base_url="https://ntfy.sh", topic="test-topic", transport=httpx.MockTransport(handler))


def test_send_posts_to_topic_with_title_header() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/test-topic"
        assert request.headers["title"] == "Alert"
        assert request.content == b"something happened"
        return httpx.Response(200)

    client = _mock_client(handler)
    client.send("something happened", title="Alert")
    client.close()


def test_send_without_title_omits_header() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "title" not in {name.lower() for name in request.headers.keys()}
        return httpx.Response(200)

    client = _mock_client(handler)
    client.send("no title here")
    client.close()


def test_notify_tool_not_registered_by_default() -> None:
    siris = SirisAI(settings=Settings())
    assert siris.tools.get("notify") is None


def test_notify_tool_registered_when_topic_configured() -> None:
    siris = SirisAI(settings=Settings(ntfy_topic="my-topic"))
    try:
        assert siris.tools.get("notify") is not None
    finally:
        assert siris._notify_client is not None
        siris._notify_client.close()
