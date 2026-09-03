"""Auto-discovered tools from an OpenAPI spec (milestone 32).
DynamicOpenAPITool wraps one GET operation.

Deliberately GET-only: an auto-generated tool has no hand-reviewed
judgment behind it the way every other tool in app/plugins/builtin/ does
-- milestone 12's permission levels assumed a human deliberately picked
SAFE_ACTION/CONFIRM_REQUIRED for a specific tool's specific behavior.
Generating a tool that could POST/PUT/DELETE against an arbitrary API
from spec text alone, then handing it any permission at all, would be
actively misleading about what it's safe to let a model call unattended.
So this milestone only discovers GET operations (app/integrations/openapi.py),
and every DynamicOpenAPITool is unconditionally ToolPermission.READ --
there's no mechanism yet to "upgrade" one, on purpose, until there's real
review behind doing that.

build_dynamic_tools() builds ONE shared httpx.Client per integration
rather than one per operation, on purpose: constructing an httpx.Client
turned out to be surprisingly expensive in a heavily-proxied environment
(~50ms each here) -- an integration with 30 GET operations building 30
separate clients added over a second to every SirisAI() construction
once a few integrations had accumulated. One client per integration,
shared by every operation's tool, fixes that and is also just more
correct (one connection pool per API, not thirty).
"""

from typing import Any
from urllib.parse import quote

import httpx

from app.integrations.openapi import DiscoveredOperation
from app.plugins.base import Tool, ToolPermission


class DynamicOpenAPITool(Tool):
    permission = ToolPermission.READ

    def __init__(self, integration_name: str, operation: DiscoveredOperation, client: httpx.Client) -> None:
        self.integration_name = integration_name
        self.name = f"{integration_name}_{operation.name}"
        self.description = f"[{integration_name}] {operation.description}"
        self.parameters = operation.parameters_schema
        self._operation = operation
        self._client = client

    def run(self, **kwargs: Any) -> Any:
        path = self._operation.path
        query_params: dict[str, Any] = {}
        for key, value in kwargs.items():
            if key in self._operation.path_params:
                path = path.replace(f"{{{key}}}", quote(str(value), safe=""))
            else:
                query_params[key] = value
        response = self._client.get(path, params=query_params)
        response.raise_for_status()
        return response.json()


def build_dynamic_tools(
    integration_name: str,
    operations: list[DiscoveredOperation],
    base_url: str,
    auth_header_name: str | None = None,
    auth_header_value: str | None = None,
    transport: httpx.BaseTransport | None = None,
) -> tuple[list[DynamicOpenAPITool], httpx.Client]:
    """One shared client, one DynamicOpenAPITool per operation against it.
    The caller (SirisAI.register_discovered_integration_tools) owns the
    returned client's lifecycle -- these tools don't close it
    themselves, since they don't own it exclusively."""
    headers = {auth_header_name: auth_header_value} if auth_header_name and auth_header_value else {}
    client = httpx.Client(base_url=base_url.rstrip("/"), headers=headers, timeout=15.0, transport=transport)
    tools = [DynamicOpenAPITool(integration_name, operation, client) for operation in operations]
    return tools, client
