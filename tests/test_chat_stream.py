import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
from fastapi.testclient import TestClient

from app.core.siris import get_siris
from app.llm.base import LLMChunk, LLMResponse, ToolCall
from app.llm.metering import MeteredLLMClient
from app.llm.ollama import OllamaClient
from app.llm.streaming import generate_with_events
from app.main import app
from tests.test_cost_dashboard import _isolated_metrics
from tests.test_permissions import _ConfirmRequiredTool


def _collect(async_iter: AsyncIterator[Any]) -> list[Any]:
    async def go() -> list[Any]:
        return [item async for item in async_iter]

    return asyncio.run(go())


def _events(response: Any) -> list[dict[str, Any]]:
    return [json.loads(line[len("data: ") :]) for line in response.text.splitlines() if line.startswith("data: ")]


# -- OllamaClient.stream_generate: Ollama's newline-delimited JSON stream


def _ndjson(*lines: dict[str, Any]) -> bytes:
    return ("\n".join(json.dumps(line) for line in lines) + "\n").encode()


def _ollama(handler: Any) -> OllamaClient:
    return OllamaClient(base_url="http://ollama.local", model="m", transport=httpx.MockTransport(handler))


def test_ollama_stream_yields_thinking_then_content_then_assembled_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["stream"] is True
        return httpx.Response(
            200,
            content=_ndjson(
                {"message": {"role": "assistant", "content": "", "thinking": "Let me"}, "done": False},
                {"message": {"role": "assistant", "content": "", "thinking": " think"}, "done": False},
                {"message": {"role": "assistant", "content": "It is ", "thinking": ""}, "done": False},
                {"message": {"role": "assistant", "content": "391."}, "done": False},
                {"message": {"role": "assistant", "content": ""}, "done": True, "prompt_eval_count": 20, "eval_count": 7},
            ),
        )

    chunks = _collect(_ollama(handler).stream_generate([{"role": "user", "content": "17*23?"}]))

    assert [c.thinking for c in chunks if c.thinking] == ["Let me", " think"]
    assert [c.content for c in chunks if c.content] == ["It is ", "391."]
    final = chunks[-1].response
    assert final is not None
    assert final.text == "It is 391."
    assert final.tool_calls == []
    assert (final.prompt_tokens, final.completion_tokens) == (20, 7)
    assert all(c.response is None for c in chunks[:-1])


def test_ollama_stream_collects_tool_calls() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=_ndjson(
                {
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [{"function": {"name": "echo", "arguments": {"message": "hi"}}}],
                    },
                    "done": False,
                },
                {"message": {"role": "assistant", "content": ""}, "done": True},
            ),
        )

    chunks = _collect(_ollama(handler).stream_generate([{"role": "user", "content": "x"}], tools=[{"type": "function"}]))

    final = chunks[-1].response
    assert final is not None
    assert final.tool_calls == [ToolCall(name="echo", arguments={"message": "hi"})]
    assert final.text is None


def test_ollama_stream_raises_on_http_error() -> None:
    client = _ollama(lambda request: httpx.Response(500))
    try:
        _collect(client.stream_generate([{"role": "user", "content": "x"}]))
    except httpx.HTTPStatusError:
        return
    raise AssertionError("expected HTTPStatusError")


# -- LLMClient.stream_generate default + generate_with_events


class _PlainClient:
    """No stream_generate at all -- like the fake clients used across the test suite."""

    async def generate(self, messages: list[dict[str, Any]], *, tools: Any = None, **kwargs: object) -> LLMResponse:
        return LLMResponse(text="plain answer")


class _StreamingClient:
    async def generate(self, messages: list[dict[str, Any]], *, tools: Any = None, **kwargs: object) -> LLMResponse:
        raise AssertionError("generate() should not be used when streaming")

    async def stream_generate(
        self, messages: list[dict[str, Any]], *, tools: Any = None, **kwargs: object
    ) -> AsyncIterator[LLMChunk]:
        yield LLMChunk(thinking="hmm")
        yield LLMChunk(content="ans")
        yield LLMChunk(response=LLMResponse(text="ans"))


def test_generate_with_events_without_callback_is_plain_generate() -> None:
    response = asyncio.run(generate_with_events(_PlainClient(), [], None))
    assert response.text == "plain answer"


def test_generate_with_events_falls_back_for_clients_that_cannot_stream() -> None:
    seen: list[dict[str, Any]] = []

    async def on_event(event: dict[str, Any]) -> None:
        seen.append(event)

    response = asyncio.run(generate_with_events(_PlainClient(), [], on_event))

    assert response.text == "plain answer"
    assert seen == [{"type": "content", "delta": "plain answer"}]


def test_generate_with_events_forwards_thinking_and_content_and_returns_final() -> None:
    seen: list[dict[str, Any]] = []

    async def on_event(event: dict[str, Any]) -> None:
        seen.append(event)

    response = asyncio.run(generate_with_events(_StreamingClient(), [], on_event))

    assert response.text == "ans"
    assert seen == [{"type": "thinking", "delta": "hmm"}, {"type": "content", "delta": "ans"}]


def test_base_class_stream_generate_default_wraps_generate() -> None:
    from app.llm.base import LLMClient

    class Simple(LLMClient):
        async def generate(self, messages: list[dict[str, Any]], *, tools: Any = None, **kwargs: object) -> LLMResponse:
            return LLMResponse(text="hello")

    chunks = _collect(Simple().stream_generate([]))
    assert [c.content for c in chunks] == ["hello", ""]
    assert chunks[-1].response is not None and chunks[-1].response.text == "hello"


def test_metered_client_records_usage_from_a_streamed_response() -> None:
    class Inner(_StreamingClient):
        async def stream_generate(
            self, messages: list[dict[str, Any]], *, tools: Any = None, **kwargs: object
        ) -> AsyncIterator[LLMChunk]:
            yield LLMChunk(content="ok")
            yield LLMChunk(response=LLMResponse(text="ok", prompt_tokens=30, completion_tokens=12))

    metrics = _isolated_metrics()
    chunks = _collect(MeteredLLMClient(Inner(), metrics).stream_generate([]))

    assert chunks[0].content == "ok"
    assert metrics.total("llm_tokens_prompt") == 30.0
    assert metrics.total("llm_tokens_completion") == 12.0
    assert metrics.total("llm_tokens_total") == 42.0


# -- POST /siris/chat/stream


class _StreamingTextLLM:
    async def generate(self, messages: list[dict[str, Any]], *, tools: Any = None, **kwargs: object) -> LLMResponse:
        raise AssertionError("expected the streaming path")

    async def stream_generate(
        self, messages: list[dict[str, Any]], *, tools: Any = None, **kwargs: object
    ) -> AsyncIterator[LLMChunk]:
        yield LLMChunk(thinking="The user said hello.")
        yield LLMChunk(content="Hi ")
        yield LLMChunk(content="there!")
        yield LLMChunk(response=LLMResponse(text="Hi there!"))

    async def aclose(self) -> None:
        pass


class _ToolThenTextLLM:
    """Calls `echo` when offered tools, answers in text on the follow-up call."""

    async def generate(self, messages: list[dict[str, Any]], *, tools: Any = None, **kwargs: object) -> LLMResponse:
        if tools:
            return LLMResponse(tool_calls=[ToolCall(name="echo", arguments={"message": "ping"})])
        return LLMResponse(text="The tool echoed it back.")

    async def aclose(self) -> None:
        pass


class _FailingLLM:
    async def generate(self, messages: list[dict[str, Any]], *, tools: Any = None, **kwargs: object) -> LLMResponse:
        raise RuntimeError("backend down")

    async def aclose(self) -> None:
        pass


class _ConfirmToolLLM:
    async def generate(self, messages: list[dict[str, Any]], *, tools: Any = None, **kwargs: object) -> LLMResponse:
        if tools:
            return LLMResponse(tool_calls=[ToolCall(name="dangerous_action", arguments={"target": "prod"})])
        return LLMResponse(text="Done, it's acted on.")

    async def aclose(self) -> None:
        pass


def _post_stream(client: TestClient, llm: object, path: str, body: dict[str, Any]) -> httpx.Response:
    fake_siris = get_siris()
    fake_siris.llm = llm  # type: ignore[assignment]
    if fake_siris.tools.get("dangerous_action") is None:
        fake_siris.tools.register(_ConfirmRequiredTool())
    app.dependency_overrides[get_siris] = lambda: fake_siris
    try:
        return client.post(path, json=body)
    finally:
        app.dependency_overrides.pop(get_siris, None)


def test_stream_endpoint_sends_status_thinking_content_then_final(client: TestClient) -> None:
    response = _post_stream(client, _StreamingTextLLM(), "/siris/chat/stream", {"prompt": "hello"})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = _events(response)

    types = [e["type"] for e in events]
    assert types[0] == "status" and events[0]["message"] == "Gathering context"
    assert {"type": "status", "message": "Thinking"} in events
    assert {"type": "thinking", "delta": "The user said hello."} in events
    assert [e["delta"] for e in events if e["type"] == "content"] == ["Hi ", "there!"]
    assert types[-1] == "final"
    assert events[-1]["response"]["response"] == "Hi there!"
    assert events[-1]["response"]["conversation_id"]


def test_stream_endpoint_falls_back_for_clients_that_cannot_stream(client: TestClient) -> None:
    class Plain:
        async def generate(self, messages: list[dict[str, Any]], *, tools: Any = None, **kwargs: object) -> LLMResponse:
            return LLMResponse(text="plain")

        async def aclose(self) -> None:
            pass

    events = _events(_post_stream(client, Plain(), "/siris/chat/stream", {"prompt": "hi"}))

    assert events[-1]["type"] == "final"
    assert events[-1]["response"]["response"] == "plain"


def test_stream_endpoint_reports_tool_progress(client: TestClient) -> None:
    events = _events(_post_stream(client, _ToolThenTextLLM(), "/siris/chat/stream", {"prompt": "ping it"}))

    types = [e["type"] for e in events]
    assert types.index("tool_start") < types.index("tool_end") < types.index("final")
    assert {"type": "tool_start", "name": "echo", "arguments": {"message": "ping"}} in events
    assert {"type": "tool_end", "name": "echo", "status": "ok"} in events
    assert {"type": "status", "message": "Writing answer"} in events
    assert events[-1]["response"]["response"] == "The tool echoed it back."
    assert events[-1]["response"]["tool_call"]["name"] == "echo"


def test_stream_endpoint_matches_non_streaming_response_shape(client: TestClient) -> None:
    streamed = _events(_post_stream(client, _ToolThenTextLLM(), "/siris/chat/stream", {"prompt": "ping it"}))[-1]["response"]
    plain = _post_stream(client, _ToolThenTextLLM(), "/siris/chat", {"prompt": "ping it"}).json()

    streamed.pop("conversation_id")
    plain.pop("conversation_id")
    assert streamed == plain


def test_stream_endpoint_emits_error_event_when_llm_fails(client: TestClient) -> None:
    events = _events(_post_stream(client, _FailingLLM(), "/siris/chat/stream", {"prompt": "hi"}))

    assert events[-1] == {"type": "error", "status": 502, "detail": "LLM backend call failed."}
    assert not any(e["type"] == "final" for e in events)


def test_stream_endpoint_surfaces_confirmation_required_then_confirm_stream_runs_it(client: TestClient) -> None:
    events = _events(_post_stream(client, _ConfirmToolLLM(), "/siris/chat/stream", {"prompt": "do it"}))

    assert {"type": "tool_end", "name": "dangerous_action", "status": "needs_confirmation"} in events
    final = events[-1]["response"]
    assert final["confirmation_required"] == {"name": "dangerous_action", "arguments": {"target": "prod"}}

    confirm_events = _events(
        _post_stream(
            client,
            _ConfirmToolLLM(),
            "/siris/chat/confirm/stream",
            {"conversation_id": final["conversation_id"], "tool_name": "dangerous_action", "arguments": {"target": "prod"}},
        )
    )
    assert {"type": "tool_end", "name": "dangerous_action", "status": "ok"} in confirm_events
    assert confirm_events[-1]["type"] == "final"
    assert confirm_events[-1]["response"]["tool_call"]["result"] == {"acted_on": "prod"}


def test_confirm_stream_404s_for_unknown_conversation(client: TestClient) -> None:
    response = _post_stream(
        client,
        _ConfirmToolLLM(),
        "/siris/chat/confirm/stream",
        {"conversation_id": "does-not-exist", "tool_name": "dangerous_action", "arguments": {}},
    )
    assert response.status_code == 404


def test_stream_endpoint_persists_the_conversation_like_chat(client: TestClient) -> None:
    events = _events(_post_stream(client, _StreamingTextLLM(), "/siris/chat/stream", {"prompt": "remember me"}))
    conversation_id = events[-1]["response"]["conversation_id"]

    history = client.get(f"/siris/conversations/{conversation_id}").json()
    assert [(m["role"], m["content"]) for m in history] == [("user", "remember me"), ("assistant", "Hi there!")]
