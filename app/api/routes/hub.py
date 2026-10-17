"""The hub contract, v1 (app/hub/contract.py): what SirisOS builds its home
screen, daily brief, search, inbox and controls on.

    GET  /siris/hub/v1                          who/what: user, guest mode, features
    GET  /siris/hub/v1/brief                    the day, as data (no LLM)
    GET  /siris/hub/v1/widgets                  car, parcels, energy, protocols, cameras, autonomy
    GET  /siris/hub/v1/search?q=                notes, past chats, memories
    GET  /siris/hub/v1/calendar?start=&end=     every event in a date range (up to 62 days)
    GET  /siris/hub/v1/forecast?days=7          the daily forecast at home, today first
    GET  /siris/hub/v1/attention                the attention feed
    GET  /siris/hub/v1/attention/stream         ...live (Server-Sent Events)
    POST /siris/hub/v1/attention/{id}/act       {"action": "approve"} etc.
    POST /siris/hub/v1/attention/{id}/dismiss
    GET  /siris/hub/v1/protocols/{name}         what running it would do now
    POST /siris/hub/v1/protocols/{name}/run     {"confirmed": true}
    POST /siris/hub/v1/protocols/undo           {"confirmed": true}
    POST /siris/hub/v1/cameras/{camera}/look    {"question": "..."}
    POST /siris/hub/v1/llm/complete             a plain completion through the model router

Every response is validated against its contract model before it leaves.
Requests act as X-Siris-User like the rest of the API, so per-user health,
memories and chats follow whoever is signed in to SirisOS.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.core.auth import require_api_key
from app.core.siris import SirisAI, get_siris
from app.core.user_context import resolve_user_id
from app.hub import attention, compose
from app.hub import contract as c
from app.plugins.base import ConfirmationRequired

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/siris/hub/v1", tags=["hub"], dependencies=[Depends(require_api_key), Depends(resolve_user_id)])

KEEPALIVE_SECONDS = 15.0


def _client_name(request: Request) -> str:
    return request.headers.get("x-siris-client") or "the app"


@router.get("", response_model=c.HubInfo)
def hub_info(siris: SirisAI = Depends(get_siris)) -> c.HubInfo:
    return compose.info(siris)


@router.get("/brief", response_model=c.Brief)
async def hub_brief(siris: SirisAI = Depends(get_siris)) -> c.Brief:
    return await asyncio.to_thread(compose.brief, siris)


@router.get("/widgets", response_model=c.Widgets)
async def hub_widgets(siris: SirisAI = Depends(get_siris)) -> c.Widgets:
    return await asyncio.to_thread(compose.widgets, siris)


@router.get("/search", response_model=c.SearchResults)
async def hub_search(q: str = Query(min_length=2, max_length=200), limit: int = Query(8, ge=1, le=25), siris: SirisAI = Depends(get_siris)) -> c.SearchResults:
    return await asyncio.to_thread(compose.search, siris, q.strip(), limit)


@router.get("/calendar", response_model=c.CalendarRange)
async def hub_calendar(
    start: date = Query(description="First day, YYYY-MM-DD"),
    end: date = Query(description="The day after the last day, YYYY-MM-DD"),
    siris: SirisAI = Depends(get_siris),
) -> c.CalendarRange:
    if end <= start or (end - start).days > compose.MAX_CALENDAR_DAYS:
        raise HTTPException(status_code=422, detail=f"end must be after start, and at most {compose.MAX_CALENDAR_DAYS} days later.")
    return await asyncio.to_thread(compose.calendar_range, siris, start, end)


@router.get("/forecast", response_model=c.Forecast)
async def hub_forecast(days: int = Query(7, ge=1, le=7), siris: SirisAI = Depends(get_siris)) -> c.Forecast:
    return await asyncio.to_thread(compose.forecast, siris, days)


# -- attention ------------------------------------------------------------------


def _attention_list(siris: SirisAI, status: str | None) -> c.AttentionList:
    return c.AttentionList(items=siris.attention.list(status), counts=c.AttentionCounts(**siris.attention.counts()))


@router.get("/attention", response_model=c.AttentionList)
async def attention_list(status: str = Query("open", pattern="^(open|done|dismissed|all)$"), siris: SirisAI = Depends(get_siris)) -> c.AttentionList:
    await asyncio.to_thread(attention.sync_all, siris)
    return await asyncio.to_thread(_attention_list, siris, None if status == "all" else status)


@router.get("/attention/stream")
async def attention_stream(
    request: Request, max_events: int = Query(0, ge=0, le=1000), siris: SirisAI = Depends(get_siris)
) -> StreamingResponse:
    """A snapshot of every open item, then each change as it happens, as
    `data: <AttentionEvent JSON>` lines; a comment every 15 s keeps proxies
    from closing an idle stream. `max_events` (default 0 = forever) ends the
    stream after that many events, for a client that only wants a peek."""
    await asyncio.to_thread(attention.sync_all, siris)
    queue = siris.attention.subscribe()
    snapshot = await asyncio.to_thread(_attention_list, siris, "open")

    async def events() -> AsyncIterator[str]:
        try:
            first = c.AttentionEvent(type="snapshot", items=snapshot.items, counts=snapshot.counts)
            yield f"data: {first.model_dump_json()}\n\n"
            sent = 1
            last_sync = time.monotonic()
            while not max_events or sent < max_events:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), KEEPALIVE_SECONDS)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    if time.monotonic() - last_sync > 60:
                        last_sync = time.monotonic()
                        await asyncio.to_thread(attention.sync_all, siris)
                    continue
                yield f"data: {c.AttentionEvent(**event).model_dump_json()}\n\n"
                sent += 1
        finally:
            siris.attention.unsubscribe(queue)

    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("/attention/{item_id}/act", response_model=c.AttentionActResult)
async def attention_act(item_id: int, body: c.AttentionActRequest, request: Request, siris: SirisAI = Depends(get_siris)) -> c.AttentionActResult:
    try:
        item, result = await asyncio.to_thread(attention.act, siris, item_id, body.action, _client_name(request))
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except attention.UnknownAction as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ConfirmationRequired as exc:  # shouldn't happen (actions confirm), but never 500 on it
        raise HTTPException(status_code=409, detail=f"{exc} needs confirmation") from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Attention action failed")
        raise HTTPException(status_code=502, detail=f"That didn't work: {str(exc)[:300]}") from exc
    return c.AttentionActResult(item=item, result=json.loads(json.dumps(result, default=str)))


@router.post("/attention/{item_id}/dismiss", response_model=c.AttentionActResult)
async def attention_dismiss(item_id: int, request: Request, siris: SirisAI = Depends(get_siris)) -> c.AttentionActResult:
    return await attention_act(item_id, c.AttentionActRequest(action="dismiss"), request, siris)


# -- protocols ------------------------------------------------------------------


def _need_protocols(siris: SirisAI) -> None:
    if siris.tools.get("run_protocol") is None:
        raise HTTPException(status_code=404, detail="Protocols aren't available (Home Assistant isn't configured).")


def _preview(siris: SirisAI, name: str) -> c.ProtocolPreview:
    from app.protocols import service

    found = service.preview(siris, name)
    if not found.get("found"):
        raise HTTPException(status_code=404, detail=f"Unknown protocol: {name}")
    return c.ProtocolPreview(
        name=found["protocol"],
        description=found.get("description") or "",
        steps=[c.ProtocolStep(label=s["step"], tool=s["tool"], would_run=bool(s["will_run"]), reason=s.get("because") or None) for s in found["steps"]],
    )


@router.get("/protocols/{name}", response_model=c.ProtocolPreview)
def protocol_preview(name: str, siris: SirisAI = Depends(get_siris)) -> c.ProtocolPreview:
    _need_protocols(siris)
    return _preview(siris, name)


@router.post("/protocols/undo", response_model=c.ProtocolRunResult)
async def protocol_undo(body: c.ProtocolRunRequest, request: Request, siris: SirisAI = Depends(get_siris)) -> c.ProtocolRunResult:
    _need_protocols(siris)
    if not body.confirmed:
        raise HTTPException(status_code=409, detail="Undoing a protocol changes locks, covers and modes: confirm first.")
    result = await asyncio.to_thread(
        siris.run_tool, "undo_last_protocol", {}, confirmed=True, trigger=f"hub: the user undid the last protocol in {_client_name(request)}"
    )
    if not result.get("undone"):
        raise HTTPException(status_code=409, detail=result.get("reason") or "Nothing to undo.")
    steps = result.get("steps") or []
    return c.ProtocolRunResult(
        name=str(result.get("protocol")),
        ran=[f"{s['tool']} {s['arguments'].get('service', '')}".strip() for s in steps if s["status"] == "ok"],
        failed=[f"{s['tool']}: {s.get('error')}" for s in steps if s["status"] != "ok"],
        can_undo=False,
    )


@router.post("/protocols/{name}/run", response_model=c.ProtocolRunResult)
async def protocol_run(name: str, body: c.ProtocolRunRequest, request: Request, siris: SirisAI = Depends(get_siris)) -> c.ProtocolRunResult:
    _need_protocols(siris)
    preview = _preview(siris, name)
    if not body.confirmed:
        raise HTTPException(status_code=409, detail={"message": "Protocols change locks, covers and modes: confirm first.", "preview": preview.model_dump()})
    result = await asyncio.to_thread(
        siris.run_tool, "run_protocol", {"name": preview.name}, confirmed=True, trigger=f"hub: the user ran protocol '{preview.name}' in {_client_name(request)}"
    )
    steps = result.get("steps") or []
    return c.ProtocolRunResult(
        name=result["protocol"],
        ran=[s["step"] for s in steps if s["status"] == "ok"],
        skipped=[f"{s['step']} ({s.get('reason')})" for s in steps if s["status"] == "skipped"],
        failed=[f"{s['step']}: {s.get('error')}" for s in steps if s["status"] == "error"],
        can_undo=bool(result.get("undo_available")),
    )


# -- cameras ----------------------------------------------------------------------


@router.post("/cameras/{camera}/look", response_model=c.CameraLook)
async def camera_look(camera: str, body: c.CameraLookRequest, request: Request, siris: SirisAI = Depends(get_siris)) -> c.CameraLook:
    if siris.tools.get("frigate_look_at_camera") is None:
        raise HTTPException(status_code=404, detail="Looking at cameras needs Frigate and a vision model (SIRISAI_VISION_MODEL).")
    args: dict[str, Any] = {"camera": camera}
    if body.question:
        args["question"] = body.question
    try:
        result = await asyncio.to_thread(siris.run_tool, "frigate_look_at_camera", args, trigger=f"hub: the user asked to look at {camera} in {_client_name(request)}")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Couldn't look at {camera}: {str(exc)[:300]}") from exc
    return c.CameraLook(camera=result.get("camera") or camera, description=str(result.get("description") or ""), question=result.get("question"))


# -- llm ------------------------------------------------------------------------------


@router.post("/llm/complete", response_model=c.LLMCompleteResult)
async def llm_complete(body: c.LLMCompleteRequest, siris: SirisAI = Depends(get_siris)) -> c.LLMCompleteResult:
    """One plain completion (no tools, no memory, no conversation) through
    SirisAI's model router, so other apps don't keep their own model
    connection. Recorded in the audit log under the caller's task name."""
    assert siris.model_router is not None
    role, llm = siris.model_router.pick(siris.llm, body.prompt, role=body.role)
    messages = ([{"role": "system", "content": body.system}] if body.system else []) + [{"role": "user", "content": body.prompt}]
    start = time.monotonic()
    try:
        response = await llm.generate(messages)
    except Exception as exc:
        siris.audit.record("hub_llm_complete", {"task": body.task, "role": role}, error=str(exc)[:500],
                           duration_ms=(time.monotonic() - start) * 1000, trigger=f"hub: {body.task} asked for a completion")
        raise HTTPException(status_code=502, detail=f"The model didn't answer: {str(exc)[:300]}") from exc
    text = (response.text or "").strip()
    siris.audit.record("hub_llm_complete", {"task": body.task, "role": role, "prompt_chars": len(body.prompt)}, result={"chars": len(text)},
                       duration_ms=(time.monotonic() - start) * 1000, trigger=f"hub: {body.task} asked for a completion")
    if not text:
        raise HTTPException(status_code=502, detail="The model returned nothing.")
    model = getattr(llm, "model", None) or getattr(getattr(llm, "_inner", None), "model", None)
    return c.LLMCompleteResult(text=text, role=role, model=str(model) if model else None)
