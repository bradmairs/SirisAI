"""Run a saved scene (a Jarvis-concept brainstorm item: "first-class
scenes, not raw HA automation YAML") -- executes every step of a
Scene (app/scenes/service.py) in order, exactly as if
home_assistant_call_service had been called once per step.

A Skill rather than a raw Tool because it composes multiple
`siris.run_tool()` calls -- exactly what the Skill abstraction exists
for (see app/skills/base.py's module docstring); a raw Tool subclass
should never hold a generic `siris` reference just to call
`siris.run_tool()` itself.

CONFIRM_REQUIRED (set at registration in app/core/siris.py, same as any
other SkillTool): a scene can call anything home_assistant_call_service
could, across as many steps as it has, so it gets the same
human-in-the-loop tier as that tool's own default (before its
no-confirm-domains allowlist) -- the single confirmation of *running
this scene* covers every step it's about to replay, the same reasoning
RollbackLastActionSkill's own docstring uses for its one confirmation
covering its one, already-identified inner action.
"""

from typing import TYPE_CHECKING, Any

from app.plugins.base import ToolVerification
from app.skills.base import Skill

if TYPE_CHECKING:
    from app.core.siris import SirisAI


class RunSceneSkill(Skill):
    name = "run_scene"
    description = (
        "Run a saved scene, using its id from list_scenes -- executes every one of its steps "
        "in order, exactly as if home_assistant_call_service had been called once per step. "
        "Call with dry_run to preview every step before confirming."
    )
    parameters = {
        "type": "object",
        "properties": {
            "scene_id": {"type": "integer", "description": "The scene's id, from list_scenes."},
        },
        "required": ["scene_id"],
    }

    def run(self, siris: "SirisAI", scene_id: int = 0, **kwargs: Any) -> Any:
        scene = siris.scenes.get_scene(scene_id)
        if scene is None:
            raise ValueError(f"Unknown scene id: {scene_id}")

        results = []
        for index, step in enumerate(scene["steps"]):
            result = siris.run_tool(
                "home_assistant_call_service",
                {
                    "domain": step["domain"],
                    "service": step["service"],
                    "entity_id": step.get("entity_id"),
                    "service_data": step.get("service_data"),
                },
                # Not a second live confirmation per step -- the one
                # confirmation of running this named scene *is* the
                # approval for every step it's already documented as
                # containing (see this file's module docstring).
                confirmed=True,
                trigger=f"run_scene: step {index + 1} of scene '{scene['name']}' (id={scene_id})",
            )
            results.append(result)
        return {"scene_id": scene_id, "name": scene["name"], "step_results": results}

    def dry_run(self, siris: "SirisAI", scene_id: int = 0, **kwargs: Any) -> Any:
        """What running `scene_id` would do, without doing it: the
        scene's name and every step it would replay, in order -- so
        whoever is about to confirm a multi-step scene sees the whole
        thing at once rather than approving it blind."""
        scene = siris.scenes.get_scene(scene_id)
        if scene is None:
            return {"scene_id": scene_id, "name": None, "steps": [], "found": False}
        return {
            "scene_id": scene_id,
            "name": scene["name"],
            "steps": scene["steps"],
            "found": True,
        }

    def verify(self, siris: "SirisAI", arguments: dict[str, Any], result: Any) -> ToolVerification:
        """No single correct end state to check here -- a scene's steps
        can span any mix of domains/services, each with its own idea of
        "did this work" (or none at all, e.g. a media_player toggle with
        no stable resting state), the same "genuinely ambiguous outcome"
        gap HomeAssistantCallServiceTool.verify() already raises
        NotImplementedError for on its own less-than-fully-deterministic
        calls -- a scene is several of those at once, so it inherits the
        same honesty rather than fabricating a verdict."""
        raise NotImplementedError(f"{self.name} does not support verify")
