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
    GET  /siris/hub/v1/protocols                every protocol with its steps, and what a step can do (the editor)
    PUT  /siris/hub/v1/protocols/{name}         create or change one (a built-in's name saves your version)
    DELETE /siris/hub/v1/protocols/{name}       delete yours (a built-in you'd changed comes back)
    GET  /siris/hub/v1/protocols/{name}         what running it would do now
    POST /siris/hub/v1/protocols/{name}/run     {"confirmed": true}
    POST /siris/hub/v1/protocols/undo           {"confirmed": true}
    POST /siris/hub/v1/cameras/{camera}/look    {"question": "..."}
    POST /siris/hub/v1/llm/complete             a plain completion through the model router
    GET  /siris/hub/v1/presence                 home or away, from the last presence check, and the phones that count
    PUT  /siris/hub/v1/presence/phones          the phones added from SirisOS that count as home (4.4)
    GET  /siris/hub/v1/reminders                pending reminders ("remind me at 7")
    POST /siris/hub/v1/reminders/{id}/done      cancel one that's no longer needed
    GET  /siris/hub/v1/email/with?address=      latest emails from these people (meeting prep)
    GET  /siris/hub/v1/network                  UniFi gateway, switches, APs and clients with live rates (SirisOS house view)
    POST /siris/hub/v1/network/{mac}/block      {blocked}: block a client, or let it back in (SirisOS network alerts)
    GET  /siris/hub/v1/energy/usage             Home Assistant's power sensors and kWh meters (SirisOS house view)
    GET  /siris/hub/v1/controls                 lights, garage doors, locks and scenes (SirisOS Home)
    POST /siris/hub/v1/controls/{entity}        {action, confirmed}: one of them (garage/unlock ask first)
    POST /siris/hub/v1/scenes/{id}/run          a Siris or Home Assistant scene
    GET  /siris/hub/v1/email/about?q=           latest emails with these words in the subject (project pages)
    GET  /siris/hub/v1/upkeep                   backups and whether an update is waiting
    GET  /siris/hub/v1/tracking                 what SirisAI watches, and what you've stopped it tracking (5.51)
    PUT  /siris/hub/v1/tracking                 {kind, name, tracked, why?}
    GET  /siris/hub/v1/health/days?days=7       health metrics per day, oldest first (up to 400 days)

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

from app.core import approvals
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


LONG_ACTION_SECONDS = 20.0


def _report_late(siris: SirisAI, item: dict[str, Any] | None, action: str, task: "asyncio.Future[Any]") -> None:
    title = (item or {}).get("title") or "that"
    try:
        closed, _ = task.result()
        outcome = (closed or {}).get("outcome")
        siris.attention.raise_item(f"late:{(item or {}).get('id')}:{action}", "notice", f"Done: {title}", source="actions", severity="info",
                                   body=outcome or f"'{action}' finished.")
    except Exception as exc:  # noqa: BLE001 - say so; nobody is waiting on the request any more
        logger.warning("A slow attention action failed: %s", exc)
        siris.attention.raise_item(f"late:{(item or {}).get('id')}:{action}", "alert", f"That didn't work: {title}", source="actions",
                                   severity="attention", body=str(exc)[:300])


@router.post("/attention/{item_id}/act", response_model=c.AttentionActResult)
async def attention_act(item_id: int, body: c.AttentionActRequest, request: Request, siris: SirisAI = Depends(get_siris)) -> c.AttentionActResult:
    # A slow action (a reboot, a big update on a SirisAI that doesn't run it
    # in the background) answers after LONG_ACTION_SECONDS instead of holding
    # the request until the phone gives up ("Load failed"); it carries on,
    # and a notice says how it went (3.72).
    work = asyncio.ensure_future(asyncio.to_thread(attention.act, siris, item_id, body.action, _client_name(request), body.body, body.pin))
    done, _ = await asyncio.wait({work}, timeout=LONG_ACTION_SECONDS)
    if work not in done:
        before = siris.attention.get(item_id)
        work.add_done_callback(lambda t: _report_late(siris, before, body.action, t))
        return c.AttentionActResult(item=before, result={"pending": True, "message": "Still working on it; a notice will say how it went."})
    try:
        item, result = work.result()
    except approvals.ApprovalExpired as exc:
        raise HTTPException(status_code=410, detail=str(exc)) from exc
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


# -- tracking (5.51) ------------------------------------------------------------------


@router.get("/tracking", response_model=c.Tracking)
def tracking_list(siris: SirisAI = Depends(get_siris)) -> c.Tracking:
    from app.homelab import tracking

    return c.Tracking.model_validate(tracking.overview(siris))


@router.put("/tracking", response_model=c.Tracking)
def tracking_change(body: c.TrackingChange, siris: SirisAI = Depends(get_siris)) -> c.Tracking:
    """Stop (tracked: false) or start tracking something again."""
    from app.homelab import tracking

    try:
        if body.tracked:
            tracking.resume(siris, body.kind, body.name)
        else:
            tracking.stop(siris, body.kind, body.name, body.why)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return c.Tracking.model_validate(tracking.overview(siris))


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


@router.get("/protocols", response_model=c.ProtocolCatalogue)
def protocol_catalogue(siris: SirisAI = Depends(get_siris)) -> c.ProtocolCatalogue:
    from app.protocols import editor

    _need_protocols(siris)
    return c.ProtocolCatalogue.model_validate(editor.catalogue(siris))


@router.put("/protocols/{name}", response_model=c.ProtocolDef)
def protocol_save(name: str, body: c.ProtocolSave, siris: SirisAI = Depends(get_siris)) -> c.ProtocolDef:
    from app.protocols import editor

    _need_protocols(siris)
    try:
        saved = editor.save(siris, name, body.description, [s.model_dump(exclude_none=True) for s in body.steps])
    except editor.Invalid as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return c.ProtocolDef.model_validate(saved)


@router.put("/protocols/{name}/switches", response_model=c.ProtocolSwitched)
def protocol_switches(name: str, body: c.ProtocolSwitches, siris: SirisAI = Depends(get_siris)) -> c.ProtocolSwitched:
    """5.11: turn a protocol on or off, and its notifications on or off."""
    from app.protocols import service

    _need_protocols(siris)
    try:
        return c.ProtocolSwitched.model_validate(service.set_switches(siris, name, body.active, body.notify))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None


@router.delete("/protocols/{name}", response_model=c.ProtocolDeleted)
def protocol_delete(name: str, siris: SirisAI = Depends(get_siris)) -> c.ProtocolDeleted:
    from app.protocols import editor

    _need_protocols(siris)
    return c.ProtocolDeleted.model_validate(editor.delete(siris, name))


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
    from app.protocols import service as protocol_service

    if not protocol_service.is_active(siris, preview.name):
        raise HTTPException(status_code=409, detail=f"{preview.name.replace('_', ' ').capitalize()} is turned off: turn it on first.")
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


# -- home controls ------------------------------------------------------------------


@router.get("/network", response_model=c.Network)
async def hub_network(siris: SirisAI = Depends(get_siris)) -> c.Network:
    try:
        return await asyncio.to_thread(compose.network, siris)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"UniFi didn't answer: {str(exc)[:200]}") from exc


@router.post("/network/{mac}/block", response_model=c.NetworkBlockResult)
async def hub_network_block(mac: str, body: c.NetworkBlockRequest, request: Request, siris: SirisAI = Depends(get_siris)) -> c.NetworkBlockResult:
    trigger = f"hub: the user {'blocked' if body.blocked else 'unblocked'} {mac} in {_client_name(request)}"
    try:
        return await asyncio.to_thread(compose.block, siris, mac, body.blocked, trigger)
    except compose.ControlRefused as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"UniFi didn't do it: {str(exc)[:200]}") from exc


@router.get("/energy/usage", response_model=c.EnergyUsage)
async def hub_energy_usage(siris: SirisAI = Depends(get_siris)) -> c.EnergyUsage:
    try:
        return await asyncio.to_thread(compose.energy_usage, siris)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Home Assistant didn't answer: {str(exc)[:200]}") from exc


@router.get("/controls", response_model=c.Controls)
async def hub_controls(siris: SirisAI = Depends(get_siris)) -> c.Controls:
    try:
        return await asyncio.to_thread(compose.controls, siris)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Home Assistant didn't answer: {str(exc)[:200]}") from exc


@router.post("/controls/{entity_id}", response_model=c.ControlResult)
async def hub_control(entity_id: str, body: c.ControlRequest, request: Request, siris: SirisAI = Depends(get_siris)) -> c.ControlResult:
    trigger = f"hub: the user tapped {body.action.replace('_', ' ')} on {entity_id} in {_client_name(request)}"
    try:
        return await asyncio.to_thread(compose.control, siris, entity_id, body.action, body.confirmed, trigger)
    except compose.ControlRefused as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
    except PermissionError as exc:  # SIRISAI_APPROVAL_PIN_TOOLS names Home Assistant: approve it in SirisAI
        raise HTTPException(status_code=403, detail=f"{exc}. Approve it in SirisAI.") from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Home Assistant didn't do it: {str(exc)[:200]}") from exc


@router.post("/scenes/{scene_id}/run", response_model=c.ControlResult)
async def hub_scene(scene_id: str, request: Request, siris: SirisAI = Depends(get_siris)) -> c.ControlResult:
    try:
        return await asyncio.to_thread(compose.run_scene, siris, scene_id, f"hub: the user ran scene {scene_id} in {_client_name(request)}")
    except compose.ControlRefused as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=f"{exc}. Approve it in SirisAI.") from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"The scene didn't run: {str(exc)[:200]}") from exc


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


# -- the day ---------------------------------------------------------------------


@router.get("/presence", response_model=c.Presence)
async def hub_presence(siris: SirisAI = Depends(get_siris)) -> c.Presence:
    return await asyncio.to_thread(compose.presence, siris)


@router.put("/presence/phones", response_model=c.Presence)
async def hub_presence_phones(body: c.PresenceDevices, siris: SirisAI = Depends(get_siris)) -> c.Presence:
    """The phones added from SirisOS (4.4) that count as someone home, as a whole list."""
    from app.skills.builtin.presence import set_added

    set_added(siris, body.added)
    return await asyncio.to_thread(compose.presence, siris)


@router.get("/reminders", response_model=c.ReminderList)
async def hub_reminders(siris: SirisAI = Depends(get_siris)) -> c.ReminderList:
    return await asyncio.to_thread(compose.reminders, siris)


@router.post("/reminders/{reminder_id}/done", response_model=c.ReminderDone)
async def hub_reminder_done(reminder_id: int, siris: SirisAI = Depends(get_siris)) -> c.ReminderDone:
    if not await asyncio.to_thread(siris.reminders.cancel, reminder_id):
        raise HTTPException(status_code=404, detail="No pending reminder with that id.")
    return c.ReminderDone(id=reminder_id, status="cancelled")


@router.get("/email/with", response_model=c.EmailWith)
async def hub_email_with(
    address: list[str] = Query(min_length=1, max_length=8),
    limit: int = Query(5, ge=1, le=10),
    siris: SirisAI = Depends(get_siris),
) -> c.EmailWith:
    try:
        return await asyncio.to_thread(compose.email_with, siris, [a.strip() for a in address if a.strip()], limit)
    except Exception as exc:
        logger.info("email_with failed", exc_info=True)
        raise HTTPException(status_code=502, detail=f"Email didn't answer: {str(exc)[:200]}") from exc


@router.get("/email/about", response_model=c.EmailWith)
async def hub_email_about(
    q: list[str] = Query(min_length=1, max_length=6),
    limit: int = Query(5, ge=1, le=10),
    siris: SirisAI = Depends(get_siris),
) -> c.EmailWith:
    try:
        return await asyncio.to_thread(compose.email_about, siris, [w.strip() for w in q if w.strip()], limit)
    except Exception as exc:
        logger.info("email_about failed", exc_info=True)
        raise HTTPException(status_code=502, detail=f"Email didn't answer: {str(exc)[:200]}") from exc


@router.get("/upkeep", response_model=c.Upkeep)
async def hub_upkeep(siris: SirisAI = Depends(get_siris)) -> c.Upkeep:
    return await asyncio.to_thread(compose.upkeep, siris)


@router.get("/health/overview", response_model=c.HealthOverview)
async def hub_health_overview(siris: SirisAI = Depends(get_siris)) -> c.HealthOverview:
    return await asyncio.to_thread(compose.health_overview, siris)


@router.get("/health/workouts", response_model=c.HealthWorkouts)
async def hub_health_workouts(days: int = Query(42, ge=1, le=400), siris: SirisAI = Depends(get_siris)) -> c.HealthWorkouts:
    return await asyncio.to_thread(compose.health_workouts, siris, days)


@router.get("/health/days", response_model=c.HealthDays)
async def hub_health_days(days: int = Query(7, ge=1, le=400), siris: SirisAI = Depends(get_siris)) -> c.HealthDays:
    return await asyncio.to_thread(compose.health_days, siris, days)
