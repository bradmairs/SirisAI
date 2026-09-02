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

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


class Tool(ABC):
    """Base class for a single callable capability."""

    name: str
    description: str

    @abstractmethod
    def run(self, **kwargs: Any) -> Any:
        """Execute the tool and return a JSON-serializable result."""
        raise NotImplementedError


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
