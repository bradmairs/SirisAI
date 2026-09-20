"""Cron overview route: GET /siris/cron_jobs, the direct-read equivalent of
the cron_list_jobs tool -- same relationship GET /siris/scenes has to
list_scenes. Always registered; returns an empty list when
SIRISAI_CRON_HOSTS isn't set (siris.cron_client is None), the same
"empty = feature not engaged" convention used elsewhere (e.g.
MetricsDashboardResponse.power_draw).

Toggling a job stays reachable only via POST /siris/tools/cron_set_job_enabled/run
-- same generic-endpoint pattern the Gaps/Scenes tabs already use, rather
than a dedicated route.
"""

from typing import Any

from fastapi import APIRouter, Depends

from app.core.auth import require_api_key
from app.core.siris import SirisAI, get_siris
from app.core.user_context import resolve_user_id

router = APIRouter(
    prefix="/siris/cron_jobs", tags=["cron"], dependencies=[Depends(require_api_key), Depends(resolve_user_id)]
)


@router.get("")
def list_cron_jobs(siris: SirisAI = Depends(get_siris)) -> list[dict[str, Any]]:
    if siris.cron_client is None:
        return []
    return siris.cron_client.list_jobs()
