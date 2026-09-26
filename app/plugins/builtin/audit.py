"""Explainable autonomy (milestone 28): a tool the model can call to
answer "why did you do X" from the audit log itself
(app/audit/service.py), instead of guessing after the fact from whatever
happens to be in the conversation.

TrustLedgerTool (a later hardening pass) answers a different question --
not "why did this one call happen" but "which tools' verify() checks
actually hold up in practice" -- by rolling up the same per-call
verified/verification_detail data this file's other tool already reads
one entry at a time.
"""

from typing import Any

from app.audit.service import AuditLogStore
from app.plugins.base import Tool, ToolPermission


class ExplainRecentActionTool(Tool):
    name = "explain_recent_action"
    description = (
        "Explain why SirisAI took a specific action recently, e.g. 'why did you restart plex' -> "
        "tool_name='docker_restart_container', argument_contains='plex'. Omit tool_name to explain "
        "the most recent action of any kind. Looks up the most "
        "recent matching audit log entries and returns each one's trigger (what caused it), "
        "evidence (supporting data that justified it, if any was recorded), policy (what "
        "allowed it to run without asking first), and -- for tools that support it, e.g. "
        "docker_restart_container -- verified/verification_detail: an independent, deterministic "
        "check of whether the action's result actually reflects success, not just that the call "
        "itself didn't error."
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
        "required": [],
    }

    def __init__(self, audit: AuditLogStore) -> None:
        self._audit = audit

    def run(self, tool_name: str = "", argument_contains: str | None = None, **kwargs: Any) -> Any:
        if tool_name:
            return self._audit.find_recent(tool_name, argument_contains=argument_contains)
        # No tool named ("explain the last thing you did"): the most recent
        # calls that changed something, skipping plain lookups.
        recent = self._audit.list_recent(limit=40)
        actions = [e for e in recent if not str(e.get("policy") or "").startswith("READ")]
        return actions[:5]


class TrustLedgerTool(Tool):
    name = "trust_ledger"
    description = (
        "Show how well each tool's verify() check actually holds up in practice -- attempts, "
        "how many came back verified vs. not, and a success rate, worst first. Only covers "
        "tools that define verify() and have been called at least once with it attempted; a "
        "tool with no verify() at all has nothing to report here. Use this to spot a tool "
        "whose real-world verification is failing more than it's passing, not just whether "
        "one specific recent call succeeded (explain_recent_action is for that)."
    )
    permission = ToolPermission.READ
    parameters = {
        "type": "object",
        "properties": {
            "days": {
                "type": "integer",
                "description": "Only count runs from the last N days. Defaults to 3, so old failures from a bug that's since been fixed don't drag the score down.",
            }
        },
        "required": [],
    }

    def __init__(self, audit: AuditLogStore) -> None:
        self._audit = audit

    def run(self, days: int = 3, **kwargs: Any) -> Any:
        return self._audit.verification_stats_by_tool(days=days)
