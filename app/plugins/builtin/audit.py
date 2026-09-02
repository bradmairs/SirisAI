"""Explainable autonomy (milestone 28): a tool the model can call to
answer "why did you do X" from the audit log itself
(app/audit/service.py), instead of guessing after the fact from whatever
happens to be in the conversation.
"""

from typing import Any

from app.audit.service import AuditLogStore
from app.plugins.base import Tool, ToolPermission


class ExplainRecentActionTool(Tool):
    name = "explain_recent_action"
    description = (
        "Explain why SirisAI took a specific action recently, e.g. 'why did you restart plex' -> "
        "tool_name='docker_restart_container', argument_contains='plex'. Looks up the most "
        "recent matching audit log entries and returns each one's trigger (what caused it), "
        "evidence (supporting data that justified it, if any was recorded), and policy (what "
        "allowed it to run without asking first)."
    )
    permission = ToolPermission.READ
    parameters = {
        "type": "object",
        "properties": {
            "tool_name": {
                "type": "string",
                "description": "The tool whose recent runs to explain, e.g. 'docker_restart_container'.",
            },
            "argument_contains": {
                "type": "string",
                "description": "Optional substring filter on the call's arguments, e.g. 'plex'.",
            },
        },
        "required": ["tool_name"],
    }

    def __init__(self, audit: AuditLogStore) -> None:
        self._audit = audit

    def run(self, tool_name: str = "", argument_contains: str | None = None, **kwargs: Any) -> Any:
        return self._audit.find_recent(tool_name, argument_contains=argument_contains)
