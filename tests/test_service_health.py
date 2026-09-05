import httpx

from app.plugins.builtin.service_health import CheckServiceEndpointTool


def test_check_service_endpoint_reports_reachable_with_status_and_latency() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "http://plex.local:32400/"
        return httpx.Response(200)

    result = CheckServiceEndpointTool(transport=httpx.MockTransport(handler)).run(url="http://plex.local:32400/")

    assert result["url"] == "http://plex.local:32400/"
    assert result["reachable"] is True
    assert result["status_code"] == 200
    assert result["error"] is None
    assert isinstance(result["elapsed_ms"], float)


def test_check_service_endpoint_reports_non_2xx_status_as_still_reachable() -> None:
    """Reachable just means "got an HTTP response" -- a 500 is still a
    real answer from a real server, distinct from not reaching it at all."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    result = CheckServiceEndpointTool(transport=httpx.MockTransport(handler)).run(url="http://plex.local:32400/")

    assert result["reachable"] is True
    assert result["status_code"] == 503


def test_check_service_endpoint_reports_unreachable_on_connection_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    result = CheckServiceEndpointTool(transport=httpx.MockTransport(handler)).run(url="http://plex.local:32400/")

    assert result["reachable"] is False
    assert result["status_code"] is None
    assert "connection refused" in result["error"]


def test_check_service_endpoint_reports_unreachable_on_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("timed out", request=request)

    result = CheckServiceEndpointTool(transport=httpx.MockTransport(handler)).run(
        url="http://plex.local:32400/", timeout_seconds=0.01
    )

    assert result["reachable"] is False
    assert result["error"] is not None


def test_check_service_endpoint_never_returns_the_response_body() -> None:
    """Deliberately narrow -- see the module docstring: this must never
    become a general-purpose "fetch a URL and read it back" tool."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"super secret internal dashboard contents")

    result = CheckServiceEndpointTool(transport=httpx.MockTransport(handler)).run(url="http://plex.local:32400/")

    assert "super secret" not in str(result)
    assert set(result.keys()) == {"url", "reachable", "status_code", "elapsed_ms", "error"}
