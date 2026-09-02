"""Routes for the SirisAI core service itself.

Kept separate from `health.py` on purpose: `/health` is an infra-level
liveness probe, `/siris/status` is app-level introspection (what's
registered, what backend is active) that will grow as the core does.
"""

from fastapi import APIRouter, Depends

from app.core.siris import SirisAI, get_siris

router = APIRouter(prefix="/siris", tags=["siris"])


@router.get("/status")
def status(siris: SirisAI = Depends(get_siris)) -> dict[str, object]:
    return siris.status()
