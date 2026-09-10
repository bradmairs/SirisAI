"""Scene storage: named, ordered bundles of Home Assistant service calls
(see app/db/models.py's Scene for the full concept). Same "own
short-lived session per call" pattern as MemoryStore/RoutineStore.

Deliberately just a store -- nothing here calls Home Assistant or
decides when to run a scene. That's RunSceneSkill
(app/skills/builtin/run_scene.py), the same "store vs. what acts on it"
split GoalStore draws with the planner and GeofenceReminderStore draws
with GeofenceReminderCheckSkill.

Not user-scoped, same reasoning as RoutineStore: a scene is shared home
infrastructure, not personal state.
"""

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.db.models import Scene


class SceneStore:
    def __init__(self, session_factory: sessionmaker) -> None:
        self._session_factory = session_factory

    def create_scene(self, name: str, steps: list[dict[str, Any]]) -> dict[str, Any]:
        with self._session_factory() as session:
            scene = Scene(name=name, steps=json.dumps(steps))
            session.add(scene)
            session.commit()
            session.refresh(scene)
            return self._serialize(scene)

    def list_scenes(self) -> list[dict[str, Any]]:
        with self._session_factory() as session:
            stmt = select(Scene).order_by(Scene.created_at.desc(), Scene.id.desc())
            return [self._serialize(row) for row in session.scalars(stmt).all()]

    def get_scene(self, scene_id: int) -> dict[str, Any] | None:
        with self._session_factory() as session:
            scene = session.get(Scene, scene_id)
            return self._serialize(scene) if scene is not None else None

    def delete_scene(self, scene_id: int) -> bool:
        with self._session_factory() as session:
            scene = session.get(Scene, scene_id)
            if scene is None:
                return False
            session.delete(scene)
            session.commit()
            return True

    @staticmethod
    def _serialize(scene: Scene) -> dict[str, Any]:
        return {
            "id": scene.id,
            "name": scene.name,
            "steps": json.loads(scene.steps),
            "created_at": scene.created_at.isoformat(),
        }
