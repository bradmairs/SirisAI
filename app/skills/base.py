"""Skill interface: a workflow that chains multiple tool calls into one
reusable unit (e.g. a health-check skill running several individual
checks and summarizing the result).

Mechanically a Skill is not a new kind of registry entry -- `SkillTool`
below adapts it to the existing `Tool` interface (app/plugins/base.py) and
it's registered in the same `ToolRegistry` as everything else. That means
a skill is offered to the model exactly like a tool, and gets the same
permission gating and audit logging for free: `Skill.run()` calls
`siris.run_tool()` for each step it needs, so if any of those steps is
itself CONFIRM_REQUIRED or RESTRICTED, that's still enforced independently
-- a skill can't launder a dangerous action through composition.

To add a skill: subclass `Skill` in app/skills/builtin/, wrap it in a
`SkillTool` and register that in `SirisAI._register_builtin_tools`, same
as any other tool.
"""

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

from app.plugins.base import Tool, ToolPermission

if TYPE_CHECKING:
    from app.core.siris import SirisAI


class Skill(ABC):
    """Base class for a multi-step workflow over existing tools."""

    name: str
    description: str
    parameters: dict[str, Any]

    @abstractmethod
    def run(self, siris: "SirisAI", **kwargs: Any) -> Any:
        """Execute the skill (typically via `siris.run_tool(...)` calls)
        and return a JSON-serializable result."""
        raise NotImplementedError


class SkillTool(Tool):
    """Adapts a `Skill` to the `Tool` interface."""

    def __init__(self, skill: Skill, siris: "SirisAI", permission: ToolPermission = ToolPermission.READ) -> None:
        self.name = skill.name
        self.description = skill.description
        self.parameters = skill.parameters
        self.permission = permission
        self._skill = skill
        self._siris = siris

    def run(self, **kwargs: Any) -> Any:
        return self._skill.run(self._siris, **kwargs)
