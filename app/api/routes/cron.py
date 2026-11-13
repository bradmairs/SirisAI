"""Cron routes.

    GET    /siris/cron_jobs              each job in each host's own crontab (unchanged)
    GET    /siris/cron_jobs/overview     per server: name, time offset, uptime; every
                                         job (its crontab and the system files) with
                                         the schedule in words, next and last run
    POST   /siris/cron_jobs/hosts        {"target": "brad@192.168.0.127", "pin": ...}: add a server
    DELETE /siris/cron_jobs/hosts/{label}  ?pin=...: stop watching one

Adding or removing a server is a settings change (SIRISAI_CRON_HOSTS), so it
takes the PIN when one is set and is refused in guest mode. Toggling a job
stays the cron_set_job_enabled tool, which asks first.
"""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.api.routes.settings import _guard
from app.core.auth import require_api_key
from app.core.siris import SirisAI, get_siris
from app.core.user_context import resolve_user_id
from app.cron import hosts as cron_hosts

router = APIRouter(
    prefix="/siris/cron_jobs", tags=["cron"], dependencies=[Depends(require_api_key), Depends(resolve_user_id)]
)


class AddHost(BaseModel):
    target: str = Field(..., max_length=300, description="user@host or user@host:port")
    pin: str | None = Field(None, max_length=32)


@router.get("")
def list_cron_jobs(siris: SirisAI = Depends(get_siris)) -> list[dict[str, Any]]:
    if siris.cron_client is None:
        return []
    return siris.cron_client.list_jobs()


@router.get("/overview")
def cron_overview(cached: bool = False, siris: SirisAI = Depends(get_siris)) -> dict[str, Any]:
    """`?cached=true` (6.38): the last overview SirisAI kept (the cron watch or the
    last visit), at once, for the Cron tab to show while it asks the servers."""
    from app.cron import watch

    if cached:
        kept = watch.cached(siris) if siris.cron_client is not None else None
        return {"hosts": (kept or {}).get("hosts") or [], "at": (kept or {}).get("at"), "cached": True,
                "public_key": cron_hosts.public_key()}
    hosts = watch.refresh(siris) if siris.cron_client is not None else []
    return {"hosts": hosts, "at": None, "cached": False, "public_key": cron_hosts.public_key()}


@router.post("/hosts")
def add_cron_host(body: AddHost, siris: SirisAI = Depends(get_siris)) -> dict[str, Any]:
    _guard(siris, body.pin)
    try:
        return cron_hosts.add(siris, body.target)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except ConnectionError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from None


@router.delete("/hosts/{label}")
def remove_cron_host(label: str, pin: str | None = None, siris: SirisAI = Depends(get_siris)) -> dict[str, Any]:
    _guard(siris, pin)
    try:
        return cron_hosts.remove(siris, label)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
