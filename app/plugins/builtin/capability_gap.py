"""Capability gap tools (milestone 31, extended): how the model reports
"I can't do that yet" as a persisted, queryable development spec instead
of a chat reply that evaporates. report_capability_gap/list_capability_gaps
are always registered -- this is core reasoning infrastructure, not an
optional integration.

`CapabilityGapStore.list_gaps()` has been readable by a human all along,
via `GET /siris/capability_gaps` -- but nothing let the model itself
query it mid-conversation, e.g. "what have you already flagged as
missing?" `list_capability_gaps` closes that parity gap the same way
every other list_* tool already gives the model chat-time access to
something a REST endpoint could already show a human.

`dismiss_capability_gap` and `resolve_capability_gap` (a Jarvis-concept
follow-up) close a gap this whole feature had until now: once the Gaps
tab (a UI milestone) made the backlog actually visible, there was no way
to clear an entry off it. `dismiss_capability_gap` is always registered
(closing a gap with "not doing this" needs nothing but this store).
`resolve_capability_gap` is registered only alongside Hermes Agent
(app/hermes/client.py) -- a separate agent on the same host with its own
terminal/git access to this repo -- since it delegates the actual
implementation work to Hermes rather than doing it itself: SirisAI is
the thing running *inside* the container a fix might need to rebuild, so
it was never going to write and merge its own replacement from in here.

SAFE_ACTION rather than CONFIRM_REQUIRED: the user explicitly chose
"draft and merge without a review prompt, but gate the rebuild/restart
separately" when this was scoped, so invoking resolve_capability_gap
itself is the deliberate act (a human, or the model when explicitly
asked, has to call it) -- once called, the merge decision is Hermes's
own job to make correctly, not a second confirmation SirisAI adds on
top. The actual redeploy is a separate, always-CONFIRM_REQUIRED tool
(RebuildAndRestartTool, app/plugins/builtin/hermes.py) precisely because
that step still needs a human's explicit go-ahead every time.

Hermes's own free-text reply is the only signal available for "did this
actually merge" -- SirisAI has no independent git access to check itself
(that access lives on Hermes's side). Rather than guessing from prose,
the instruction asks Hermes to end its reply with an exact marker line
(RESULT: MERGED / RESULT: NOT_MERGED: <reason>) that gets parsed with a
plain substring check; a reply with neither marker is treated as *not*
merged, never assumed successful -- the same "don't fabricate a verdict
you can't actually verify" discipline `Tool.verify()`'s own convention
already uses elsewhere in this codebase.

A successful merge fires a push notification (when a notify channel is
configured) -- otherwise nothing would ever tell a human a fix is ready
to deploy; they'd have to remember to go check the Gaps tab. When
SIRISAI_PUBLIC_BASE_URL is also set, the notification carries a one-tap
"Rebuild now" ntfy action button posting straight to
`POST /siris/tools/rebuild_and_restart/run` with `confirmed: true` --
the exact same `http` action-button mechanism
`RoutineRunner._notify_pending_confirmation()`
(app/routines/runner.py) already uses for a routine's own pending
confirmations, reused here rather than inventing a second way to build
one. Calling `NotifyClient.send()` directly rather than going through
`siris.run_tool("notify", ...)` mirrors how this tool already calls
`HermesClient` directly -- a Tool's own single unit of work doesn't need
a second, nested audit-log entry for a side effect of the same call.
"""

import json
from typing import Any

from app.capabilities.service import CapabilityGapStore
from app.hermes.client import HermesClient
from app.notify.client import NotifyClient
from app.plugins.base import Tool, ToolPermission

_MERGED_MARKER = "RESULT: MERGED"
_NOT_MERGED_MARKER = "RESULT: NOT_MERGED"


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
        gap = self._gaps.report_gap(requested_capability, suggested_integration, suggested_tool_spec)
        # Said out loud so the reply doesn't imply the request is being
        # worked on (found live: "I have initiated the search").
        gap["note"] = "This only records the missing capability. Nothing was searched, built or fixed."
        return gap


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
            "status": {
                "type": "string",
                "description": "Optional: only gaps in this status -- 'open', 'resolved', 'failed', or 'dismissed'.",
            },
        },
        "required": [],
    }

    def __init__(self, gaps: CapabilityGapStore) -> None:
        self._gaps = gaps

    def run(self, limit: int = 50, status: str | None = None, **kwargs: Any) -> Any:
        return self._gaps.list_gaps(limit, status)


class DismissCapabilityGapTool(Tool):
    name = "dismiss_capability_gap"
    description = (
        "Close a reported capability gap without building anything for it -- e.g. it's not "
        "worth doing, or it's already covered another way. Use gap_id from "
        "list_capability_gaps. Does not involve Hermes Agent or change any code."
    )
    permission = ToolPermission.SAFE_ACTION
    parameters = {
        "type": "object",
        "properties": {
            "gap_id": {"type": "integer", "description": "The capability gap's id, from list_capability_gaps."},
        },
        "required": ["gap_id"],
    }

    def __init__(self, gaps: CapabilityGapStore) -> None:
        self._gaps = gaps

    def run(self, gap_id: int = 0, **kwargs: Any) -> Any:
        updated = self._gaps.dismiss(gap_id)
        if updated is None:
            raise ValueError(f"Unknown capability gap id: {gap_id}")
        return updated


def _build_resolve_instruction(gap: dict[str, Any]) -> str:
    lines = [
        "A SirisAI user asked for a capability SirisAI doesn't have yet. Implement it in the "
        "SirisAI repository (the same one you already have terminal/git access to), following "
        "the existing Tool/Skill conventions already used throughout the codebase (see "
        "app/plugins/base.py and app/skills/base.py, and existing tools for the pattern to "
        "match).",
        "",
        f"Requested capability: {gap['requested_capability']}",
    ]
    if gap.get("suggested_integration"):
        lines.append(f"Suggested integration: {gap['suggested_integration']}")
    if gap.get("suggested_tool_spec"):
        lines.append(f"Suggested tool shape: {json.dumps(gap['suggested_tool_spec'])}")
    lines += [
        "",
        "Work on a new branch, add tests for the new capability, and run the full test suite. "
        "Only merge to the main branch if the full suite passes. SirisAI controls real devices "
        "in a home, including things like door locks -- do not merge anything you are not "
        "confident is correct and safe; leaving the gap unresolved is always better than a bad "
        "merge.",
        "",
        "End your reply with exactly one of these two lines, and nothing after it:",
        _MERGED_MARKER,
        f"{_NOT_MERGED_MARKER}: <one-line reason>",
    ]
    return "\n".join(lines)


def _parse_hermes_result(response: str) -> tuple[bool, str]:
    """Whether Hermes reported a successful merge, and the detail to
    store either way. Never inferred from prose -- only the exact marker
    line the instruction asked for counts as a merge; anything else
    (including a plausible-sounding but unmarked "done!") is treated as
    not merged, per this module's own docstring."""
    if _MERGED_MARKER in response:
        return True, response
    if _NOT_MERGED_MARKER in response:
        return False, response
    return False, (
        "Hermes Agent's response didn't include the expected RESULT: MERGED / "
        "RESULT: NOT_MERGED marker, so this was treated as not merged rather than assumed "
        "successful:\n\n" + response
    )


class ResolveCapabilityGapTool(Tool):
    name = "resolve_capability_gap"
    description = (
        "Delegate a reported capability gap to Hermes Agent to actually implement: Hermes "
        "drafts the code on a new branch, adds tests, runs the full suite, and merges to main "
        "only if everything passes. A successful merge sends a push notification (if "
        "configured) that a fix is ready to deploy. This does NOT rebuild or restart SirisAI "
        "itself -- use rebuild_and_restart for that, as a separate, explicitly confirmed step. "
        "Use gap_id from list_capability_gaps. A gap that isn't 'open' (already resolved, "
        "failed, or dismissed) is left alone and reported back as-is rather than re-attempted."
    )
    permission = ToolPermission.CONFIRM_REQUIRED
    parameters = {
        "type": "object",
        "properties": {
            "gap_id": {"type": "integer", "description": "The capability gap's id, from list_capability_gaps."},
        },
        "required": ["gap_id"],
    }

    def __init__(
        self,
        gaps: CapabilityGapStore,
        hermes: HermesClient,
        notify: NotifyClient | None = None,
        public_base_url: str = "",
        api_key: str = "",
    ) -> None:
        self._gaps = gaps
        self._hermes = hermes
        self._notify = notify
        self._public_base_url = public_base_url
        self._api_key = api_key

    def run(self, gap_id: int = 0, **kwargs: Any) -> Any:
        gap = self._gaps.get_gap(gap_id)
        if gap is None:
            raise ValueError(f"Unknown capability gap id: {gap_id}")
        if gap["status"] != "open":
            return {
                "gap_id": gap_id,
                "status": gap["status"],
                "attempted": False,
                "detail": gap["resolution_detail"],
            }

        response = self._hermes.run_task(_build_resolve_instruction(gap))
        merged, detail = _parse_hermes_result(response)
        updated = (
            self._gaps.mark_resolved(gap_id, detail)
            if merged
            else self._gaps.mark_failed(gap_id, detail)
        )
        if merged:
            self._notify_ready_to_deploy(gap["requested_capability"])
        return {
            "gap_id": gap_id,
            "status": updated["status"],
            "attempted": True,
            "detail": updated["resolution_detail"],
        }

    def _notify_ready_to_deploy(self, requested_capability: str) -> None:
        if self._notify is None:
            return

        actions = None
        if self._public_base_url:
            headers = {"Content-Type": "application/json"}
            if self._api_key:
                headers["Authorization"] = f"Bearer {self._api_key}"
            actions = [
                {
                    "action": "http",
                    "label": "Rebuild now",
                    "url": f"{self._public_base_url.rstrip('/')}/siris/tools/rebuild_and_restart/run",
                    "method": "POST",
                    "headers": headers,
                    "body": json.dumps({"arguments": {}, "confirmed": True}),
                }
            ]

        self._notify.send(
            message=f"Fix merged for '{requested_capability}'. Rebuild & restart to deploy it.",
            title="SirisAI: fix ready to deploy",
            actions=actions,
        )
