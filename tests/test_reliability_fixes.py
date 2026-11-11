"""5.8: pushes that fit ntfy, failed pushes in the feed, presence through a
gateway restart, a broken to-do list not hiding the rest, and Ollama's
thinking switch and failover to another machine."""

import asyncio
import json
from typing import Any

import httpx
import pytest

from app.core.config import Settings
from app.core.siris import SirisAI
from app.llm.factory import think_setting
from app.llm.ollama import OllamaClient
from app.notify.client import MAX_MESSAGE_BYTES, NotifyClient, fit_message


# -- notifications -------------------------------------------------------------------------------


def test_a_long_message_is_cut_to_fit_ntfy_at_a_word() -> None:
    long = "Weather: sunny. " + "Something on the calendar today; " * 600
    out = fit_message(long)
    assert len(out.encode("utf-8")) <= MAX_MESSAGE_BYTES
    assert out.endswith("… (more in SirisOS)") and not out.endswith("calen… (more in SirisOS)")
    assert fit_message("short") == "short"


def test_a_cut_never_splits_a_multibyte_character() -> None:
    out = fit_message("°" * 5000)
    out.encode("utf-8").decode("utf-8")   # would raise on a broken character
    assert len(out.encode("utf-8")) <= MAX_MESSAGE_BYTES


def test_a_shortened_push_opens_sirisos_when_tapped() -> None:
    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200)

    client = NotifyClient("https://ntfy.example", "t", transport=httpx.MockTransport(handler), click_url="https://sirisos.lan")
    client.send("x " * 4000, title="SirisAI: daily briefing")
    client.send("short")
    assert sent[0]["click"] == "https://sirisos.lan" and len(sent[0]["message"].encode()) <= MAX_MESSAGE_BYTES
    assert "click" not in sent[1]


def test_a_push_that_fails_lands_in_the_feed_and_clears_once_one_arrives() -> None:
    status = {"code": 500}
    siris = SirisAI(settings=Settings(ntfy_topic="t"))
    siris._notify_client._client = httpx.Client(  # type: ignore[union-attr]
        base_url="https://ntfy.example", transport=httpx.MockTransport(lambda r: httpx.Response(status["code"])))
    with pytest.raises(httpx.HTTPStatusError):
        siris.run_tool("notify", {"title": "Bins", "message": "Put the bins out"})
    item = next(i for i in siris.attention.list() if i["title"] == "A push notification didn't arrive")
    assert "Put the bins out" in item["body"]
    status["code"] = 200
    siris.run_tool("notify", {"message": "ok"})
    assert all(i["title"] != "A push notification didn't arrive" for i in siris.attention.list())


# -- presence through a gateway restart ----------------------------------------------------------


def test_presence_keeps_the_last_state_while_unifi_is_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.skills.builtin import presence

    siris = SirisAI(settings=Settings(presence_device_names="iPhone - Brad"))
    siris.events.log_event("presence", entity_name=presence._PRESENCE_ENTITY_NAME, data={"home": True})
    real = siris.run_tool

    def run_tool(name: str, args: dict, **kw: Any) -> Any:
        if name == "unifi_list_clients":
            raise httpx.ConnectError("Connection refused")
        return real(name, args, **kw)

    monkeypatch.setattr(siris, "run_tool", run_tool)
    out = presence.PresenceSkill().run(siris)
    assert out == {"currently_home": True, "changed": False, "scene_activated": None, "unifi_unreachable": True}


# -- to-do lists ---------------------------------------------------------------------------------


def test_one_unreadable_todo_list_doesnt_hide_the_others() -> None:
    from app.home_assistant.client import HomeAssistantClient
    from app.plugins.builtin.home_assistant import HomeAssistantGetTodoItemsTool

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/states":
            return httpx.Response(200, json=[{"entity_id": "todo.shopping", "state": "1", "attributes": {}},
                                             {"entity_id": "todo.broken", "state": "0", "attributes": {}}])
        if json.loads(request.content)["entity_id"] == "todo.broken":
            return httpx.Response(500)
        return httpx.Response(200, json={"service_response": {"todo.shopping": {"items": [{"summary": "Milk", "status": "needs_action"}]}}})

    client = HomeAssistantClient("http://ha", "token")
    client._client = httpx.Client(base_url="http://ha", transport=httpx.MockTransport(handler))
    out = HomeAssistantGetTodoItemsTool(client).run()
    assert out[0]["list"] == "todo.shopping" and out[0]["items"][0]["summary"] == "Milk"
    assert out[1]["list"] == "todo.broken" and out[1]["items"] == [] and "500" in out[1]["error"]


# -- Ollama: thinking and failover ---------------------------------------------------------------


def _ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"message": {"content": "hi"}, "done": True})


def _client(handlers: dict[str, Any], **kw: Any) -> OllamaClient:
    client = OllamaClient("http://a:11434", "gemma4", fallback_base_urls=["http://b:11434"], **kw)
    for c in client._clients:
        host = c.base_url.host
        c._transport = httpx.MockTransport(handlers[host])  # type: ignore[attr-defined]
    return client


def test_thinking_is_sent_only_when_set() -> None:
    bodies: list[dict] = []

    def capture(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return _ok(request)

    asyncio.run(_client({"a": capture, "b": capture}, think=False).generate([{"role": "user", "content": "x"}]))
    asyncio.run(_client({"a": capture, "b": capture}).generate([{"role": "user", "content": "x"}]))
    assert bodies[0]["think"] is False and "think" not in bodies[1]
    assert think_setting("off") is False and think_setting("on") is True and think_setting("") is None


def test_a_down_machine_fails_over_to_the_next() -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    reply = asyncio.run(_client({"a": down, "b": _ok}).generate([{"role": "user", "content": "x"}]))
    assert reply.text == "hi"


def test_a_bad_request_doesnt_fail_over() -> None:
    seen: list[str] = []

    def bad(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host)
        return httpx.Response(404, json={"error": "model not found"})

    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(_client({"a": bad, "b": bad}).generate([{"role": "user", "content": "x"}]))
    assert seen == ["a"]


def test_a_stream_fails_over_only_before_anything_is_said() -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    def stream(request: httpx.Request) -> httpx.Response:
        lines = [{"message": {"content": "hel"}}, {"message": {"content": "lo"}, "done": True}]
        return httpx.Response(200, content="\n".join(json.dumps(x) for x in lines).encode())

    async def collect(client: OllamaClient) -> list[Any]:
        return [c async for c in client.stream_generate([{"role": "user", "content": "x"}])]

    chunks = asyncio.run(collect(_client({"a": down, "b": stream})))
    assert chunks[-1].response.text == "hello"
