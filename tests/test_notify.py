import json

import httpx

from app.core.config import Settings
from app.core.siris import SirisAI
from app.notify.client import NotifyClient


def _mock_client(handler) -> NotifyClient:
    return NotifyClient(base_url="https://ntfy.sh", topic="test-topic", transport=httpx.MockTransport(handler))


def test_send_posts_json_with_topic_and_title() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/"
        body = json.loads(request.content)
        assert body == {"topic": "test-topic", "message": "something happened", "title": "Alert"}
        return httpx.Response(200)

    client = _mock_client(handler)
    client.send("something happened", title="Alert")
    client.close()


def test_send_without_title_omits_it() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert "title" not in body
        return httpx.Response(200)

    client = _mock_client(handler)
    client.send("no title here")
    client.close()


def test_send_without_actions_omits_them() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert "actions" not in body
        return httpx.Response(200)

    client = _mock_client(handler)
    client.send("plain message")
    client.close()


def test_send_with_actions_includes_them() -> None:
    """Actionable push notifications (milestone 43)."""
    action = {
        "action": "http",
        "label": "Confirm",
        "url": "https://siris.example.com/siris/tools/docker_restart_container/run",
        "method": "POST",
        "headers": {"Authorization": "Bearer secret"},
        "body": json.dumps({"arguments": {"container": "plex"}, "confirmed": True}),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["actions"] == [action]
        return httpx.Response(200)

    client = _mock_client(handler)
    client.send("plex needs a restart", actions=[action])
    client.close()


def test_send_without_priority_omits_it() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert "priority" not in body
        return httpx.Response(200)

    client = _mock_client(handler)
    client.send("plain message")
    client.close()


def test_send_with_priority_includes_it() -> None:
    """Escalation tiers (milestone 44)."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["priority"] == "urgent"
        return httpx.Response(200)

    client = _mock_client(handler)
    client.send("the roof is on fire", priority="urgent")
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
