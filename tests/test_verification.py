from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.siris import SirisAI, get_siris
from app.main import app
from app.plugins.base import Tool, ToolPermission, ToolVerification


class _NoVerifyTool(Tool):
    name = "no_verify_support"
    description = "A tool that doesn't override verify()."
    permission = ToolPermission.READ
    parameters = {"type": "object", "properties": {}, "required": []}

    def run(self, **kwargs: Any) -> Any:
        return {"ran": True}


class _VerifiedSuccessTool(Tool):
    name = "verified_success"
    description = "A tool whose verify() always reports success."
    permission = ToolPermission.SAFE_ACTION
    parameters = {"type": "object", "properties": {}, "required": []}

    def run(self, **kwargs: Any) -> Any:
        return {"acted": True}

    def verify(self, arguments: dict[str, Any], result: Any) -> ToolVerification:
        return ToolVerification(verified=True, detail="Confirmed via independent check.")


class _VerifiedFailureTool(Tool):
    name = "verified_failure"
    description = "A tool whose verify() always reports failure -- the '200 OK doesn't mean healthy' case."
    permission = ToolPermission.SAFE_ACTION
    parameters = {"type": "object", "properties": {}, "required": []}

    def run(self, **kwargs: Any) -> Any:
        return {"acted": True}  # the call itself "succeeded"

    def verify(self, arguments: dict[str, Any], result: Any) -> ToolVerification:
        return ToolVerification(verified=False, detail="Independent check found it didn't actually work.")


class _BrokenVerifyTool(Tool):
    name = "broken_verify"
    description = "A tool whose verify() itself raises unexpectedly."
    permission = ToolPermission.SAFE_ACTION
    parameters = {"type": "object", "properties": {}, "required": []}

    def run(self, **kwargs: Any) -> Any:
        return {"acted": True}

    def verify(self, arguments: dict[str, Any], result: Any) -> ToolVerification:
        raise RuntimeError("verifier itself is broken")


# -- Tool.verify() default/override behavior


def test_default_verify_raises_not_implemented() -> None:
    with pytest.raises(NotImplementedError):
        _NoVerifyTool().verify({}, {"ran": True})


def test_overridden_verify_returns_a_result_without_affecting_run() -> None:
    tool = _VerifiedSuccessTool()
    assert tool.run() == {"acted": True}
    assert tool.verify({}, {"acted": True}) == ToolVerification(verified=True, detail="Confirmed via independent check.")


# -- SirisAI.run_tool() wiring


def _siris_with(tool: Tool) -> SirisAI:
    siris = SirisAI(settings=Settings())
    siris.tools.register(tool)
    return siris


def test_run_tool_records_nothing_for_a_tool_without_a_verifier() -> None:
    siris = _siris_with(_NoVerifyTool())
    siris.run_tool("no_verify_support", {})
    entry = siris.audit.list_recent(limit=1)[0]
    assert entry["verified"] is None
    assert entry["verification_detail"] is None


def test_run_tool_records_a_successful_verification() -> None:
    siris = _siris_with(_VerifiedSuccessTool())
    siris.run_tool("verified_success", {})
    entry = siris.audit.list_recent(limit=1)[0]
    assert entry["verified"] is True
    assert entry["verification_detail"] == "Confirmed via independent check."


def test_run_tool_records_a_failed_verification_without_treating_the_call_as_failed() -> None:
    """The core case this whole hardening pass is for: the tool call
    itself succeeded (no exception, a real result), but the independent
    check says it didn't actually work -- run_tool() still returns the
    result normally, it just also records that the outcome wasn't good."""
    siris = _siris_with(_VerifiedFailureTool())
    result = siris.run_tool("verified_failure", {})

    assert result == {"acted": True}  # run_tool() itself did not raise
    entry = siris.audit.list_recent(limit=1)[0]
    assert entry["error"] is None
    assert entry["verified"] is False
    assert entry["verification_detail"] == "Independent check found it didn't actually work."


def test_run_tool_tolerates_a_broken_verifier() -> None:
    siris = _siris_with(_BrokenVerifyTool())
    result = siris.run_tool("broken_verify", {})

    assert result == {"acted": True}
    entry = siris.audit.list_recent(limit=1)[0]
    assert entry["error"] is None  # the tool call itself still succeeded
    assert entry["verified"] is None  # verification couldn't be computed


# -- GET /siris/activity exposes the new fields


def test_activity_endpoint_exposes_verification_fields() -> None:
    siris = _siris_with(_VerifiedFailureTool())
    siris.run_tool("verified_failure", {})
    app.dependency_overrides[get_siris] = lambda: siris
    try:
        with TestClient(app) as client:
            response = client.get("/siris/activity", params={"limit": 1})
        assert response.status_code == 200
        entry = response.json()[0]
        assert entry["verified"] is False
        assert entry["verification_detail"] == "Independent check found it didn't actually work."
    finally:
        app.dependency_overrides.pop(get_siris, None)
