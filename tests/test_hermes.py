import json

import httpx

from app.hermes.client import HermesClient
from app.plugins.base import ToolPermission
from app.plugins.builtin.hermes import HermesRunTaskTool, HermesStatusTool


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


# -- check_status / hermes_status


def test_check_status_hits_v1_models_and_maps_fields() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/v1/models"
        return httpx.Response(200, json={"data": [{"id": "hermes"}, {"id": "hermes-coder"}]})

    client = _mock_client(handler)
    result = client.check_status()
    assert result == {"reachable": True, "configured_model": "hermes", "available_models": ["hermes", "hermes-coder"]}
    client.close()


def test_check_status_raises_when_hermes_is_unreachable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    client = _mock_client(handler)
    try:
        client.check_status()
        raise AssertionError("expected an HTTPStatusError")
    except httpx.HTTPStatusError:
        pass
    client.close()


def test_hermes_status_tool_wraps_client_result() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"id": "hermes"}]})

    client = _mock_client(handler)
    tool = HermesStatusTool(client)
    assert tool.run() == {"reachable": True, "configured_model": "hermes", "available_models": ["hermes"]}
    client.close()


def test_hermes_status_tool_is_read_permission() -> None:
    assert HermesStatusTool(_mock_client(lambda r: httpx.Response(200, json={"data": []}))).permission is (
        ToolPermission.READ
    )


# -- configurable timeout (a hardening pass)


def test_hermes_client_uses_default_timeout_when_not_configured() -> None:
    client = HermesClient(base_url="http://localhost:8420", model="hermes")
    assert client._client.timeout.read == 120.0
    client.close()


def test_hermes_client_honors_a_configured_timeout() -> None:
    client = HermesClient(base_url="http://localhost:8420", model="hermes", timeout=300.0)
    assert client._client.timeout.read == 300.0
    client.close()


def test_hermes_tools_registered_with_configured_timeout() -> None:
    from app.core.config import Settings
    from app.core.siris import SirisAI

    siris = SirisAI(settings=Settings(hermes_base_url="http://localhost:8420", hermes_timeout_seconds=45.0))
    try:
        assert siris.tools.get("hermes_status") is not None
        assert siris._hermes_client is not None
        assert siris._hermes_client._client.timeout.read == 45.0
    finally:
        assert siris._hermes_client is not None
        siris._hermes_client.close()
