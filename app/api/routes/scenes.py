"""Scene routes: GET /siris/scenes, the direct-read equivalent of the
list_scenes tool -- same relationship GET /siris/routines has to
list_routines. Unconditionally registered (SceneStore itself has nothing
to do with Home Assistant being configured, see app/scenes/service.py),
even though the create_scene/list_scenes/delete_scene/run_scene *tools*
are only registered alongside it.

Creating, deleting, and running a scene stay reachable only via
POST /siris/tools/{name}/run (create_scene/delete_scene/run_scene) rather
than a dedicated route each -- the Web UI's Scenes tab uses exactly that
generic endpoint, the same way its Gaps tab already does for
resolve_capability_gap/dismiss_capability_gap/rebuild_and_restart.
"""

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.core.auth import require_api_key
from app.core.siris import SirisAI, get_siris
from app.core.user_context import resolve_user_id

router = APIRouter(
    prefix="/siris/scenes", tags=["scenes"], dependencies=[Depends(require_api_key), Depends(resolve_user_id)]
)


class SceneResponse(BaseModel):
    id: int
    name: str
    steps: list[dict[str, Any]]
    created_at: str


@router.get("", response_model=list[SceneResponse])
def list_scenes(siris: SirisAI = Depends(get_siris)) -> list[SceneResponse]:
    return [SceneResponse(**s) for s in siris.scenes.list_scenes()]
