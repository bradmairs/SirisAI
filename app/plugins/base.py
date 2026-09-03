"""Tool/plugin interface.

A "tool" is anything SirisAI can invoke to act in the world or answer a
question outside of the LLM itself -- a homelab query, a Home Assistant
call, a search, a memory lookup, etc. Everything lives behind this same
shape so the core service (and eventually an LLM tool-use loop) can treat
them uniformly.

To add a tool:
  1. Subclass `Tool` in app/plugins/builtin/ (or an external package).
  2. Register an instance with the `SirisAI` core's `tools` registry
     (see app/core/siris.py) at startup.

Tools are intentionally synchronous-callable via `run()`; wrap blocking I/O
yourselves if a tool needs it. Keep `run()`'s return value JSON-serializable
so tool output can later be fed straight back into an LLM tool-call loop.
"""

# Deferred annotation evaluation: `ToolRegistry.list()` and `.specs()` both
# use the builtin `list` in their return annotations, and without this a
# method literally named `list` shadows the builtin for annotations defined
# after it in the class body.
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


@dataclass
class ToolVerification:
    """An independent, deterministic check of whether a tool's result
    actually reflects success -- not just that `run()` didn't raise. See
    `Tool.verify()`."""

    verified: bool
    detail: str


class ToolPermission(str, Enum):
    """How much say a tool gets to run on its own.

    READ            -- no side effects, always runs (list/get-style tools).
    SAFE_ACTION      -- has side effects but is routine/reversible (e.g.
                        restarting a known-safe service), runs immediately.
    CONFIRM_REQUIRED -- has side effects that shouldn't happen without a
                        human in the loop (e.g. controlling an arbitrary
                        physical device). `SirisAI.run_tool()` refuses to
                        execute it and raises `ConfirmationRequired`
                        instead; the caller re-submits with `confirmed=True`
                        (see `POST /siris/chat/confirm`) to actually run it.
    RESTRICTED       -- disabled outright unless the tool's name appears in
                        `SIRISAI_RESTRICTED_TOOLS_ENABLED`. For anything
                        destructive enough that it shouldn't even be offered
                        to the model by default (e.g. deleting data).
    """

    READ = "read"
    SAFE_ACTION = "safe_action"
    CONFIRM_REQUIRED = "confirm_required"
    RESTRICTED = "restricted"


class Tool(ABC):
    """Base class for a single callable capability."""

    name: str
    description: str
    # JSON Schema for `run()`'s keyword arguments, in the shape a "function"
    # tool-calling API expects (e.g. {"type": "object", "properties": {...},
    # "required": [...]}). Lets an LLM know what arguments to pass.
    parameters: dict[str, Any]
    permission: ToolPermission

    @abstractmethod
    def run(self, **kwargs: Any) -> Any:
        """Execute the tool and return a JSON-serializable result."""
        raise NotImplementedError

    def dry_run(self, **kwargs: Any) -> Any:
        """Report what `run()` would do with these arguments, without doing
        it (milestone 25). Optional -- most tools have nothing meaningful
        to simulate (a READ tool has no effect to preview; a routine
        SAFE_ACTION with no real blast radius doesn't either), so the
        default just raises. Override only where a preview is actually
        useful, e.g. a restart reporting what's currently running and what
        else depends on it (see `DockerRestartContainerTool`). Callers
        (`POST /siris/tools/{name}/run` with `dry_run: true`) treat
        `NotImplementedError` as "this tool doesn't support dry_run", not
        a real failure."""
        raise NotImplementedError(f"{self.name} does not support dry_run")

    def verify(self, arguments: dict[str, Any], result: Any) -> ToolVerification:
        """An independent, deterministic check of whether `result` actually
        reflects success -- the gap between "the call returned 200" and
        "the thing actually happened" (a hardening pass; milestone 24's
        planner explicitly left this open: "does NOT implement a separate
        deterministic verification pass that can tell 'this 200 OK doesn't
        actually mean the service is healthy' independent of the model's
        own judgment"). Optional -- most tools have no meaningful
        post-condition beyond "the call succeeded" (already covered by
        `run()` not raising: a READ lookup or a tool with no real-world
        side effect has nothing further to check), so the default just
        raises. Override only where a real gap exists between API success
        and actual outcome, e.g. a restart whose own API call can return
        200 even though the container immediately crashes back out (see
        `DockerRestartContainerTool.verify()`). `SirisAI.run_tool()`
        treats `NotImplementedError` as "this tool doesn't support
        verify", not a real failure -- same convention as `dry_run()`.
        Never raised to reject the tool call itself: this runs *after*
        `run()` already succeeded, so a `verified=False` result is
        information about the outcome, not a reason to treat the call as
        having failed."""
        raise NotImplementedError(f"{self.name} does not support verify")

    def needs_confirmation(self, arguments: dict[str, Any]) -> bool:
        """For a CONFIRM_REQUIRED tool, whether *this specific call*
        genuinely still needs confirmation. True by default -- "yes,
        exactly what CONFIRM_REQUIRED already says" -- so every existing
        CONFIRM_REQUIRED tool is unaffected without opting in; only
        consulted (by `SirisAI.run_tool()`) when `permission` is
        CONFIRM_REQUIRED, never for READ/SAFE_ACTION/RESTRICTED tools.
        Override only where a real, narrow subset of calls is genuinely
        safe to skip -- e.g. `HomeAssistantCallServiceTool` allowlisting
        specific domains via `SIRISAI_HOME_ASSISTANT_NO_CONFIRM_DOMAINS`,
        so "turn on the light" can run immediately while "unlock the
        front door" still stops for approval. Unlike `dry_run()`/
        `verify()`, this isn't an opt-in feature signaled by
        `NotImplementedError` -- there's no ambiguity in "does this tool
        need confirmation," only in which calls to it do."""
        return True


class ConfirmationRequired(Exception):
    """Raised by `SirisAI.run_tool()` for a CONFIRM_REQUIRED tool that
    hasn't been confirmed yet. Carries what was proposed so a caller can
    show/re-submit it."""

    def __init__(self, tool_name: str, arguments: dict[str, Any]) -> None:
        super().__init__(f"Tool '{tool_name}' requires confirmation before running.")
        self.tool_name = tool_name
        self.arguments = arguments


@dataclass
class ToolRegistry:
    """Holds registered tools by name."""

    _tools: dict[str, Tool] = field(default_factory=dict)

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"Tool '{tool.name}' is already registered.")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def list(self) -> list[Tool]:
        return list(self._tools.values())

    def specs(self, exclude: set[str] | None = None) -> list[dict[str, Any]]:
        """Tool schemas in OpenAI-style function-calling format, ready to
        pass to `LLMClient.generate(tools=...)`. `exclude` -- tool names to
        leave out entirely, e.g. RESTRICTED tools that aren't enabled (see
        `SirisAI.available_tool_specs()`)."""
        exclude = exclude or set()
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                },
            }
            for tool in self._tools.values()
            if tool.name not in exclude
        ]
