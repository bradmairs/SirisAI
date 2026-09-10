"""Capability gap tools (milestone 31, extended): how the model reports
"I can't do that yet" as a persisted, queryable development spec instead
of a chat reply that evaporates. Always registered -- this is core
reasoning infrastructure, not an optional integration.

`CapabilityGapStore.list_gaps()` has been readable by a human all along,
via `GET /siris/capability_gaps` -- but nothing let the model itself
query it mid-conversation, e.g. "what have you already flagged as
missing?" `list_capability_gaps` closes that parity gap the same way
every other list_* tool already gives the model chat-time access to
something a REST endpoint could already show a human.
"""

from typing import Any

from app.capabilities.service import CapabilityGapStore
from app.plugins.base import Tool, ToolPermission


class ReportCapabilityGapTool(Tool):
    name = "report_capability_gap"
    description = (
        "Call this when a request needs something you have no registered tool for (check "
        "your available tools/capabilities first) -- report what's missing instead of just "
        "saying you can't do it. Name the capability the user actually wants in "
        "requested_capability. If you can identify, from your own knowledge, a plausible "
        "real-world data source or integration that would provide it, include it in "
        "suggested_integration (e.g. 'Network UPS Tools (NUT)' for UPS battery health), along "
        "with a concrete proposed tool shape in suggested_tool_spec (e.g. "
        "{'name': 'ups_get_status', 'parameters': {...}}). This produces a spec a human -- or "
        "Claude Code, working this same repo -- can act on; it does not create the tool itself."
    )
    permission = ToolPermission.SAFE_ACTION
    parameters = {
        "type": "object",
        "properties": {
            "requested_capability": {
                "type": "string",
                "description": "What the user actually wants, in a sentence, e.g. 'check UPS battery health'.",
            },
            "suggested_integration": {
                "type": "string",
                "description": "Optional: a plausible real data source/integration that would provide this.",
            },
            "suggested_tool_spec": {
                "type": "object",
                "description": "Optional: a proposed tool name + parameter shape.",
            },
        },
        "required": ["requested_capability"],
    }

    def __init__(self, gaps: CapabilityGapStore) -> None:
        self._gaps = gaps

    def run(
        self,
        requested_capability: str = "",
        suggested_integration: str | None = None,
        suggested_tool_spec: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Any:
        return self._gaps.report_gap(requested_capability, suggested_integration, suggested_tool_spec)


class ListCapabilityGapsTool(Tool):
    name = "list_capability_gaps"
    description = (
        "List capabilities that have been reported as missing (via report_capability_gap), "
        "most recent first -- what SirisAI has wanted to do but had no tool for yet."
    )
    permission = ToolPermission.READ
    parameters = {
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "description": "Maximum number of gaps to return (most recent first). Default 50.",
            },
        },
        "required": [],
    }

    def __init__(self, gaps: CapabilityGapStore) -> None:
        self._gaps = gaps

    def run(self, limit: int = 50, **kwargs: Any) -> Any:
        return self._gaps.list_gaps(limit)
