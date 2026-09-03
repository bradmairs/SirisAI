import json

import httpx

from app.hermes.client import HermesClient
from app.plugins.base import ToolPermission
from app.plugins.builtin.hermes import HermesRunTaskTool


def _mock_client(handler) -> HermesClient:
    return HermesClient(base_url="http://localhost:8420", model="hermes", transport=httpx.MockTransport(handler))


def test_run_task_posts_openai_style_chat_completion_and_returns_content() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/chat/completions"
        parsed = json.loads(request.content)
        assert parsed["model"] == "hermes"
        assert parsed["messages"] == [{"role": "user", "content": "restart the plex container"}]
        return httpx.Response(
            200, json={"choices": [{"message": {"role": "assistant", "content": "Restarted plex successfully."}}]}
        )

    client = _mock_client(handler)
    result = client.run_task("restart the plex container")
    assert result == "Restarted plex successfully."
    client.close()


def test_run_task_sends_no_authorization_header_when_api_key_unset() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "authorization" not in request.headers
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = HermesClient(base_url="http://localhost:8420", model="hermes", transport=httpx.MockTransport(handler))
    client.run_task("do something")
    client.close()


def test_run_task_sends_bearer_token_when_api_key_set() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer secret-key"
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = HermesClient(
        base_url="http://localhost:8420",
        model="hermes",
        api_key="secret-key",
        transport=httpx.MockTransport(handler),
    )
    client.run_task("do something")
    client.close()


def test_run_task_returns_empty_string_when_no_choices() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": []})

    client = _mock_client(handler)
    assert client.run_task("do something") == ""
    client.close()


def test_hermes_run_task_tool_wraps_client_result() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "done"}}]})

    client = _mock_client(handler)
    tool = HermesRunTaskTool(client)
    assert tool.run(instruction="check disk space") == {"result": "done"}
    client.close()


def test_hermes_run_task_tool_is_safe_action_permission() -> None:
    assert HermesRunTaskTool(_mock_client(lambda r: httpx.Response(200, json={"choices": []}))).permission is (
        ToolPermission.SAFE_ACTION
    )
