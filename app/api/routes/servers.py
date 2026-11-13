"""The servers on the home network over HTTP (app/servers/): their state,
and running the watch, update and disk checks now."""

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from app.core.auth import require_api_key
from app.core.siris import SirisAI, get_siris
from app.core.user_context import resolve_user_id
from app.servers import wake, watch

router = APIRouter(prefix="/siris/servers", tags=["servers"], dependencies=[Depends(require_api_key), Depends(resolve_user_id)])


def _need_ssh(siris: SirisAI) -> None:
    if siris.ssh_client is None:
        raise HTTPException(status_code=404, detail="No servers configured (SIRISAI_SSH_HOSTS).")


@router.get("")
def servers(siris: SirisAI = Depends(get_siris)) -> list[dict[str, Any]]:
    """Each server, whether the last check reached it, and its known MAC."""
    _need_ssh(siris)
    state = siris.state.get(watch.STATE_KEY) or {}
    return [
        {**host.describe(), "mac": wake.mac_for(siris, host), **{k: v for k, v in (state.get(host.name) or {}).items() if k in ("failures", "last_ok", "last_error")}}
        for host in siris.ssh_client.hosts  # type: ignore[union-attr]
    ]


@router.post("/watch")
async def run_watch(siris: SirisAI = Depends(get_siris)) -> dict[str, Any]:
    _need_ssh(siris)
    return await asyncio.to_thread(watch.check_health, siris)


@router.post("/updates")
async def run_updates(siris: SirisAI = Depends(get_siris)) -> dict[str, Any]:
    _need_ssh(siris)
    return await asyncio.to_thread(watch.check_updates, siris)


@router.post("/disks")
async def run_disks(siris: SirisAI = Depends(get_siris)) -> dict[str, Any]:
    _need_ssh(siris)
    return await asyncio.to_thread(watch.check_disks, siris)


@router.get("/{name}/timeline")
def server_timeline(name: str, days: int = 7, siris: SirisAI = Depends(get_siris)) -> dict[str, Any]:
    """What happened to one server lately, newest first (6.38)."""
    from app.servers import timeline

    days = max(1, min(days, 30))
    return {"server": name, "days": days, "events": timeline.events(siris, name, days)}
