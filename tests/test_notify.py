import json

import httpx
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import Settings
from app.core.siris import SirisAI
from app.db.base import Base
from app.notify.client import NotifyClient
from app.notify.pending import PendingNotificationStore
from app.plugins.builtin.notify import ListPendingNotificationsTool


def _mock_client(handler) -> NotifyClient:
    return NotifyClient(
        base_url="https://ntfy.sh",
        topic="test-topic",
        transport=httpx.MockTransport(handler),
    )


def test_send_posts_json_with_topic_and_title() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/"
        body = json.loads(request.content)
        assert body == {
            "topic": "test-topic",
            "message": "something happened",
            "title": "Alert",
        }
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
        assert body["priority"] == 5  # ntfy's JSON API wants 1-5, not a name
        return httpx.Response(200)

    client = _mock_client(handler)
    client.send("the roof is on fire", priority="urgent")
    client.close()


def test_priority_names_and_numbers_map_to_ntfy_levels() -> None:
    """Found live: priority="default" made ntfy answer 400."""
    from app.notify.client import _ntfy_priority

    assert [_ntfy_priority(p) for p in ("min", "low", "default", "high", "urgent")] == [1, 2, 3, 4, 5]
    assert _ntfy_priority("4") == 4 and _ntfy_priority(2) == 2
    assert _ntfy_priority("bogus") is None and _ntfy_priority(9) is None and _ntfy_priority(None) is None


def test_unrecognised_priority_is_omitted_rather_than_sent() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "priority" not in json.loads(request.content)
        return httpx.Response(200)

    client = _mock_client(handler)
    client.send("hello", priority="whenever")
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


# -- PendingNotificationStore / list_pending_notifications (milestone 72,
# an interruption controller): "notify"-tier warnings queue here instead
# of pushing immediately -- see app/api/routes/proactive.py and
# app/skills/builtin/daily_briefing.py for where they're written and read.
#
# Not user-scoped (unlike MemoryStore) -- there's exactly one queue, not
# one per user -- so each test gets its own isolated in-memory engine
# rather than the shared test-session DB, same isolation
# test_event_timing.py/test_metrics.py already use for the same reason:
# clear()'s exact counts would otherwise depend on what other tests
# queued and never cleared.


def _isolated_store() -> PendingNotificationStore:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    return PendingNotificationStore(sessionmaker(bind=engine))


def test_list_pending_notifications_tool_always_registered() -> None:
    siris = SirisAI(settings=Settings())
    assert siris.tools.get("list_pending_notifications") is not None


def test_queue_then_list_pending_returns_it_in_order() -> None:
    store = _isolated_store()
    store.queue("first warning", severity="warning", category="resource")
    store.queue("second warning", severity="warning", category="network")

    pending = store.list_pending()

    assert [p["message"] for p in pending] == ["first warning", "second warning"]
    assert pending[0]["severity"] == "warning"
    assert pending[0]["category"] == "resource"
    assert pending[1]["category"] == "network"


def test_clear_removes_everything_and_reports_how_many() -> None:
    store = _isolated_store()
    store.queue("one", severity="warning", category="resource")
    store.queue("two", severity="warning", category="resource")

    cleared = store.clear()

    assert cleared == 2
    assert store.list_pending() == []


def test_clear_on_an_empty_queue_reports_zero() -> None:
    assert _isolated_store().clear() == 0


def test_list_pending_notifications_tool_reads_the_store() -> None:
    store = _isolated_store()
    store.queue("a queued warning", severity="warning", category="resource")

    result = ListPendingNotificationsTool(store).run()

    assert any(p["message"] == "a queued warning" for p in result)
