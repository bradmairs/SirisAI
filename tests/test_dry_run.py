from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.siris import SirisAI, get_siris
from app.main import app
from app.plugins.base import Tool, ToolPermission


class _NoDryRunTool(Tool):
    name = "no_dry_run_support"
    description = "A tool that doesn't override dry_run()."
    permission = ToolPermission.READ
    parameters = {"type": "object", "properties": {}, "required": []}

    def run(self, **kwargs: Any) -> Any:
        return {"ran": True}


class _DryRunAwareTool(Tool):
    name = "dry_run_aware"
    description = "A tool that reports what it would do."
    permission = ToolPermission.SAFE_ACTION
    parameters = {"type": "object", "properties": {"target": {"type": "string"}}, "required": ["target"]}

    def run(self, target: str = "", **kwargs: Any) -> Any:
        return {"acted_on": target}

    def dry_run(self, target: str = "", **kwargs: Any) -> Any:
        return {"would_act_on": target}


def test_default_dry_run_raises_not_implemented() -> None:
    with pytest.raises(NotImplementedError):
        _NoDryRunTool().dry_run()


def test_overridden_dry_run_reports_without_acting() -> None:
    tool = _DryRunAwareTool()
    assert tool.dry_run(target="plex") == {"would_act_on": "plex"}


def test_dry_run_endpoint_returns_501_for_unsupported_tool() -> None:
    siris = SirisAI(settings=Settings())
    siris.tools.register(_NoDryRunTool())
    app.dependency_overrides[get_siris] = lambda: siris
    try:
        with TestClient(app) as client:
            response = client.post("/siris/tools/no_dry_run_support/run", json={"dry_run": True})
        assert response.status_code == 501
    finally:
        app.dependency_overrides.pop(get_siris, None)


def test_dry_run_endpoint_returns_preview_without_running_the_tool() -> None:
    siris = SirisAI(settings=Settings())
    siris.tools.register(_DryRunAwareTool())
    app.dependency_overrides[get_siris] = lambda: siris
    try:
        with TestClient(app) as client:
            response = client.post(
                "/siris/tools/dry_run_aware/run", json={"arguments": {"target": "plex"}, "dry_run": True}
            )
        assert response.status_code == 200
        body = response.json()
        assert body["tool_call"]["result"] == {"would_act_on": "plex"}

        # dry_run has no effect -- nothing recorded to the audit log (the
        # DB is shared across the test session, so scope the check to this
        # tool's name rather than asserting the whole log is empty).
        assert not any(entry["tool_name"] == "dry_run_aware" for entry in siris.audit.list_recent(limit=200))
    finally:
        app.dependency_overrides.pop(get_siris, None)


def test_dry_run_endpoint_unknown_tool_still_404s(client: TestClient) -> None:
    response = client.post("/siris/tools/does_not_exist/run", json={"dry_run": True})
    assert response.status_code == 404
