"""Generic dependent-service reachability check -- a narrow complement to
`server_health_check` (this machine's own CPU/memory/disk) and
`docker_list_containers` (container state): whether some arbitrary HTTP(S)
endpoint the homelab depends on (Plex, a reverse proxy, anything with a
URL) is actually responding, and how long it took.

Takes the URL as a call-time argument rather than a configured integration,
so unlike the other tools in this package there's no base_url/token to set
up and it's always registered -- the model supplies whatever endpoint it's
investigating (e.g. after `docker_list_containers` shows a container
"running" but the model still wants to know whether the thing inside it is
actually serving requests).

Deliberately narrow: reports only reachability, status code, and latency --
never the response body, so this can't be turned into a general-purpose
"fetch any URL and read it back" tool through the back door. Certificate
expiry checking was considered (the user's own brainstormed list asked for
it) and left out of this pass: httpx doesn't expose the peer certificate,
so it would need a raw `ssl`/socket implementation of its own -- a
reasonable follow-up, not bundled into this narrow read here.
"""

import time
from typing import Any

import httpx

from app.plugins.base import Tool, ToolPermission

_DEFAULT_TIMEOUT_SECONDS = 5.0


class CheckServiceEndpointTool(Tool):
    name = "check_service_endpoint"
    description = (
        "Check whether an HTTP(S) endpoint is reachable and how long it took to respond -- "
        "for confirming a dependent service is actually serving requests, not just that its "
        "container is running. Reports reachability, status code, and latency only; never "
        "the response body."
    )
    permission = ToolPermission.READ
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "The URL to check, e.g. http://plex.local:32400."},
            "timeout_seconds": {
                "type": "number",
                "description": "How long to wait for a response before giving up. Defaults to 5.",
            },
        },
        "required": ["url"],
    }

    def __init__(self, transport: httpx.BaseTransport | None = None) -> None:
        # `transport` is test-only (httpx.MockTransport) -- a real call
        # needs a fresh client each time since the target host varies by
        # call, unlike every other tool here which talks to one configured
        # base_url for its whole lifetime.
        self._transport = transport

    def run(self, url: str = "", timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS, **kwargs: Any) -> Any:
        start = time.monotonic()
        try:
            with httpx.Client(transport=self._transport) as client:
                response = client.get(url, timeout=timeout_seconds, follow_redirects=True)
        except httpx.HTTPError as exc:
            return {
                "url": url,
                "reachable": False,
                "status_code": None,
                "elapsed_ms": round((time.monotonic() - start) * 1000, 1),
                "error": str(exc),
            }

        return {
            "url": url,
            "reachable": True,
            "status_code": response.status_code,
            "elapsed_ms": round((time.monotonic() - start) * 1000, 1),
            "error": None,
        }
