"""Routine routes (milestone 29): GET /siris/routines (direct-read
equivalent of the list_routines tool, same relationship
GET /siris/events has to recall_events), and
POST /siris/routines/run_due -- the endpoint an external cron/systemd
timer calls to actually run whatever's due. See app/routines/runner.py
for why running stays externally triggered rather than an in-process
scheduler: milestone 19's boundary, kept deliberately (the design
question was put to the user rather than decided silently) for this
milestone too.
"""

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.core.auth import require_api_key
from app.core.siris import SirisAI, get_siris
from app.core.user_context import resolve_user_id
from app.routines.runner import RoutineRunner

router = APIRouter(
    prefix="/siris/routines", tags=["routines"], dependencies=[Depends(require_api_key), Depends(resolve_user_id)]
)


class RoutineResponse(BaseModel):
    id: int
    name: str
    trigger_description: str
    interval_minutes: int
    actions: list[str]
    output_description: str | None
    enabled: bool
    last_run_at: str | None
    created_at: str
    updated_at: str


@router.get("", response_model=list[RoutineResponse])
def list_routines(enabled_only: bool = False, siris: SirisAI = Depends(get_siris)) -> list[RoutineResponse]:
    return [RoutineResponse(**r) for r in siris.routines.list_routines(enabled_only=enabled_only)]


class RoutineActionResult(BaseModel):
    tool: str
    result: Any
    error: str | None


class RoutineRunResult(BaseModel):
    routine_id: int
    name: str
    actions: list[RoutineActionResult]


@router.post("/run_due", response_model=list[RoutineRunResult])
def run_due_routines(siris: SirisAI = Depends(get_siris)) -> list[RoutineRunResult]:
    """Run every routine that's currently due (see RoutineStore.due_routines())
    and mark each one run. Meant to be called on a schedule from outside
    this process -- same relationship POST /siris/proactive/check already
    has to server_health_check -- frequently enough to catch the
    shortest-interval routine you've configured (e.g. every 5 minutes if
    any routine has interval_minutes=5); a routine that's due just waits
    for the next call if the external trigger runs less often than that."""
    return [RoutineRunResult(**r) for r in RoutineRunner(siris).run_due()]
