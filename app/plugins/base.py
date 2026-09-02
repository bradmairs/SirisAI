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
