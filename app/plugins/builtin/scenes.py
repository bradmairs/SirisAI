"""Scene tools (a Jarvis-concept brainstorm item: "first-class scenes,
not raw HA automation YAML") -- how the LLM creates, lists, and deletes a
named, ordered bundle of Home Assistant service calls
(app/scenes/service.py, app/db/models.py's Scene). Registered only
alongside Home Assistant itself (a scene with nowhere to run isn't worth
being able to create). Running a scene is `run_scene`
(app/skills/builtin/run_scene.py, a Skill rather than a raw Tool here --
it composes multiple `siris.run_tool()` calls, which is exactly what the
Skill abstraction exists for).

Each step is exactly home_assistant_call_service's own
domain/service/entity_id/service_data shape -- a scene doesn't invent a
second way to describe a Home Assistant action, it just stores an
ordered list of the same shape and replays it.
"""

from typing import Any

from app.plugins.base import Tool, ToolPermission
from app.scenes.service import SceneStore

_STEP_SCHEMA = {
    "type": "object",
    "properties": {
        "domain": {"type": "string", "description": "Service domain, e.g. 'light', 'media_player'."},
        "service": {"type": "string", "description": "Service to call, e.g. 'turn_on', 'turn_off'."},
        "entity_id": {"type": "string", "description": "The entity to target, e.g. 'light.living_room'."},
        "service_data": {
            "type": "object",
            "description": "Extra service-specific fields, e.g. {'brightness_pct': 50}.",
        },
    },
    "required": ["domain", "service"],
}


class CreateSceneTool(Tool):
    name = "create_scene"
    description = (
        "Save a named scene -- an ordered list of Home Assistant service calls (each shaped "
        "exactly like home_assistant_call_service's own arguments) that can later be triggered "
        "as one unit via run_scene, e.g. name='movie_night', steps=[{'domain': 'light', "
        "'service': 'turn_off', 'entity_id': 'light.living_room'}, {'domain': 'media_player', "
        "'service': 'turn_on', 'entity_id': 'media_player.tv'}]. This only saves the recipe -- "
        "it doesn't run anything yet."
    )
    permission = ToolPermission.SAFE_ACTION
    parameters = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "A short name for the scene, e.g. 'movie_night'."},
            "steps": {
                "type": "array",
                "items": _STEP_SCHEMA,
                "description": "Ordered list of Home Assistant service calls this scene runs.",
            },
        },
        "required": ["name", "steps"],
    }

    def __init__(self, scenes: SceneStore) -> None:
        self._scenes = scenes

    def run(self, name: str = "", steps: list[dict[str, Any]] | None = None, **kwargs: Any) -> Any:
        return self._scenes.create_scene(name, steps or [])


class ListScenesTool(Tool):
    name = "list_scenes"
    description = "List saved scenes and their steps."
    permission = ToolPermission.READ
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, scenes: SceneStore) -> None:
        self._scenes = scenes

    def run(self, **kwargs: Any) -> Any:
        return self._scenes.list_scenes()


class DeleteSceneTool(Tool):
    name = "delete_scene"
    description = "Delete a saved scene, using its id from list_scenes. Does not affect any devices."
    permission = ToolPermission.SAFE_ACTION
    parameters = {
        "type": "object",
        "properties": {
            "scene_id": {"type": "integer", "description": "The scene's id."},
        },
        "required": ["scene_id"],
    }

    def __init__(self, scenes: SceneStore) -> None:
        self._scenes = scenes

    def run(self, scene_id: int = 0, **kwargs: Any) -> Any:
        return {"deleted": self._scenes.delete_scene(scene_id)}
