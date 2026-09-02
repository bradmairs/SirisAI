"""Liveness/readiness check.

Deliberately dependency-free beyond a DB round-trip: this is what Docker's
HEALTHCHECK and any future orchestrator hits, so it should stay fast and
have as few ways to fail as possible.
"""

import logging

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db.session import get_db

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])


@router.get("/health")
def health(db: Session = Depends(get_db)) -> dict[str, str]:
    try:
        db.execute(text("SELECT 1"))
        db_status = "ok"
    except Exception:
        logger.exception("Health check DB probe failed")
        db_status = "unavailable"

    return {"status": "ok", "database": db_status}
