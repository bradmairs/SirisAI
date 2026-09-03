"""Automatic tool discovery from an OpenAPI spec (milestone 32).
POST /siris/integrations discovers GET operations and registers one READ
tool per operation, live on the running instance immediately and
persisted (app/integrations/service.py) so they're re-registered on every
future startup too. See app/plugins/builtin/openapi_tool.py for why this
only ever generates READ tools.
"""

import logging
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.core.auth import require_api_key
from app.core.siris import SirisAI, get_siris
from app.core.user_context import resolve_user_id
from app.integrations.openapi import discover_get_operations

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/siris/integrations",
    tags=["integrations"],
    dependencies=[Depends(require_api_key), Depends(resolve_user_id)],
)


class RegisterIntegrationRequest(BaseModel):
    name: str
    base_url: str
    # Provide exactly one of these -- spec_url is fetched once, here, at
    # registration time (this endpoint is single-user behind
    # SIRISAI_API_KEY, same trust model as every other admin-only route
    # in this app, so fetching a URL the caller supplies isn't treated as
    # a distinct SSRF concern the way it might be in a multi-tenant
    # service).
    spec_url: str | None = None
    spec: dict[str, Any] | None = None
    auth_header_name: str | None = None
    auth_header_value: str | None = None


class IntegrationResponse(BaseModel):
    id: int
    name: str
    base_url: str
    auth_header_name: str | None
    has_auth: bool
    created_at: str
    discovered_tools: list[str]


def _without_spec(integration: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in integration.items() if k not in ("spec", "auth_header_value")}


@router.post("", response_model=IntegrationResponse)
def register_integration(request: RegisterIntegrationRequest, siris: SirisAI = Depends(get_siris)) -> IntegrationResponse:
    if request.spec is not None:
        spec = request.spec
    elif request.spec_url is not None:
        try:
            response = httpx.get(request.spec_url, timeout=15.0)
            response.raise_for_status()
            spec = response.json()
        except Exception:
            logger.exception("Failed to fetch OpenAPI spec from %s", request.spec_url)
            raise HTTPException(status_code=502, detail="Failed to fetch spec_url.") from None
    else:
        raise HTTPException(status_code=422, detail="Provide either spec_url or spec.")

    operations = discover_get_operations(spec)
    if not operations:
        raise HTTPException(status_code=422, detail="No GET operations found in this spec.")

    saved = siris.integrations.save_integration(
        request.name, request.base_url, spec, request.auth_header_name, request.auth_header_value
    )

    discovered_names = siris.register_discovered_integration_tools(
        request.name, operations, request.base_url, request.auth_header_name, request.auth_header_value
    )

    return IntegrationResponse(**_without_spec(saved), discovered_tools=discovered_names)


@router.get("", response_model=list[IntegrationResponse])
def list_integrations(siris: SirisAI = Depends(get_siris)) -> list[IntegrationResponse]:
    result = []
    for integration in siris.integrations.list_integrations():
        # discovered_tools isn't stored separately -- it's the live
        # registry filtered to this integration's own naming convention
        # (every tool it generated is prefixed "{name}_").
        prefix = f"{integration['name']}_"
        tool_names = [t.name for t in siris.tools.list() if t.name.startswith(prefix)]
        result.append(IntegrationResponse(**_without_spec(integration), discovered_tools=tool_names))
    return result
