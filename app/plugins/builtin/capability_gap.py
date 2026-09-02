"""Capability gap tool (milestone 31): how the model reports "I can't do
that yet" as a persisted, queryable development spec instead of a chat
reply that evaporates. Always registered -- this is core reasoning
infrastructure, not an optional integration.
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
