"""CPD and chartership evidence drafts over HTTP (app/career/capture.py)."""

from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import PlainTextResponse

from app.career import capture
from app.core.auth import require_api_key
from app.core.siris import SirisAI, get_siris
from app.core.user_context import resolve_user_id

router = APIRouter(prefix="/siris/career", tags=["career"], dependencies=[Depends(require_api_key), Depends(resolve_user_id)])


@router.get("/drafts", response_model=None)
def drafts(format: str = "json", siris: SirisAI = Depends(get_siris)) -> Any:
    """The CPD and evidence drafts. ?format=csv gives the CPD list as CSV
    (Date, Activity, Provider, Hours, Category, Notes) to copy into
    Engineers Australia."""
    if format == "csv":
        return PlainTextResponse(capture.cpd_csv(siris), media_type="text/csv", headers={"Content-Disposition": 'attachment; filename="cpd-drafts.csv"'})
    return capture.drafts(siris)


@router.delete("/drafts/cpd")
def clear_cpd(siris: SirisAI = Depends(get_siris)) -> dict[str, int]:
    """Clear the CPD list, once it's been copied into Engineers Australia."""
    count = len(capture.drafts(siris)["cpd"])
    siris.state.delete(capture.CPD_KEY)
    return {"cleared": count}


@router.post("/capture")
def capture_now(siris: SirisAI = Depends(get_siris)) -> dict[str, Any]:
    """Look over today now, instead of waiting for the evening."""
    return capture.capture(siris)
