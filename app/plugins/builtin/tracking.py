"""stop_tracking / resume_tracking / list_untracked (5.51): tell SirisAI to stop
watching something you removed on purpose (a container), and to watch it again."""

from typing import TYPE_CHECKING, Any

from app.homelab import tracking
from app.plugins.base import Tool, ToolPermission

if TYPE_CHECKING:
    from app.core.siris import SirisAI

_ARGS = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "The container's name, e.g. open-webui (spaces and capitals don't matter)."},
        "kind": {"type": "string", "enum": list(tracking.KINDS), "description": "What it is. Default: container."},
        "why": {"type": "string", "description": "Optional: why (e.g. 'removed it on purpose')."},
    },
    "required": ["name"],
}


class StopTrackingTool(Tool):
    name = "stop_tracking"
    description = (
        "Stop tracking a container the user removed or stopped on purpose, so SirisAI no longer warns that it's down, "
        "restarts it, offers updates for it or pushes anything about it. Use for 'stop tracking X', 'I removed X on "
        "purpose', 'stop telling me X is down'. Undo with resume_tracking."
    )
    permission = ToolPermission.SAFE_ACTION
    parameters = _ARGS

    def __init__(self, siris: "SirisAI") -> None:
        self._siris = siris

    def run(self, name: str = "", kind: str = "container", why: str = "", **kwargs: Any) -> Any:
        return tracking.stop(self._siris, kind or "container", name, why)


class ResumeTrackingTool(Tool):
    name = "resume_tracking"
    description = "Track a container again that the user had asked SirisAI to stop tracking (stop_tracking)."
    permission = ToolPermission.SAFE_ACTION
    parameters = {**_ARGS, "properties": {k: v for k, v in _ARGS["properties"].items() if k != "why"}}

    def __init__(self, siris: "SirisAI") -> None:
        self._siris = siris

    def run(self, name: str = "", kind: str = "container", **kwargs: Any) -> Any:
        return tracking.resume(self._siris, kind or "container", name)


class ListUntrackedTool(Tool):
    name = "list_untracked"
    description = "What the user has asked SirisAI to stop tracking, and since when."
    permission = ToolPermission.READ
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, siris: "SirisAI") -> None:
        self._siris = siris

    def run(self, **kwargs: Any) -> Any:
        return {"untracked": tracking.overview(self._siris)["untracked"]}
