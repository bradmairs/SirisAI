"""Life admin (4.6): the document inbox, bills and subscriptions found in
email, and trips in the calendar. See app/documents, app/bills and
app/travel."""

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel

from app.core.auth import require_api_key
from app.core.siris import SirisAI, get_siris
from app.core.user_context import resolve_user_id

router = APIRouter(prefix="/siris", tags=["life"], dependencies=[Depends(require_api_key), Depends(resolve_user_id)])


@router.post("/documents")
async def add_document(file: UploadFile = File(...), siris: SirisAI = Depends(get_siris)) -> dict[str, Any]:
    """A PDF, photo or text file of a letter, bill or form: read, filed in
    Siris, and reminders set for anything due."""
    from app.documents import service

    data = await file.read(service.MAX_BYTES + 1)
    try:
        return await service.ingest(siris, file.filename or "document", file.content_type or "", data)
    except service.DocumentError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/documents")
def list_documents(q: str = "", limit: int = 30, siris: SirisAI = Depends(get_siris)) -> list[dict[str, Any]]:
    from app.documents import service

    return service.list_documents(siris, q, limit=max(1, min(limit, 100)))


@router.get("/documents/{doc_id}")
def get_document(doc_id: int, siris: SirisAI = Depends(get_siris)) -> dict[str, Any]:
    from app.documents import service

    try:
        return service.get(siris, doc_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="No such document") from exc


@router.delete("/documents/{doc_id}")
def delete_document(doc_id: int, siris: SirisAI = Depends(get_siris)) -> dict[str, Any]:
    """Removes it here (the note in Siris stays: delete that in Siris)."""
    from app.documents import service

    try:
        service.delete(siris, doc_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="No such document") from exc
    return {"deleted": doc_id}


@router.get("/bills")
def list_bills(all: bool = False, siris: SirisAI = Depends(get_siris)) -> list[dict[str, Any]]:
    """Renewals, trials ending, price rises and bills due, soonest first
    (`all`: include ones marked done or ignored, and past ones)."""
    from app.bills import service

    return service.upcoming(siris, include_closed=all)


@router.post("/bills/scan")
async def scan_bills(siris: SirisAI = Depends(get_siris)) -> dict[str, Any]:
    from app.bills import service

    return await asyncio.to_thread(service.scan, siris)


class BillStatus(BaseModel):
    status: str  # open, done, ignored


@router.post("/bills/{bill_id}/status")
def bill_status(bill_id: int, request: BillStatus, siris: SirisAI = Depends(get_siris)) -> dict[str, Any]:
    from app.bills import service

    if request.status not in ("open", "done", "ignored"):
        raise HTTPException(status_code=422, detail="status is open, done or ignored")
    try:
        bill = service.set_status(siris, bill_id, request.status)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="No such bill") from exc
    service.check(siris)
    return bill


@router.get("/travel")
async def list_trips(siris: SirisAI = Depends(get_siris)) -> list[dict[str, Any]]:
    """Trips coming up, with the weather there, a packing list and a house list."""
    from app.travel import service

    return await asyncio.to_thread(service.upcoming, siris)


@router.post("/travel/away")
def travel_away(on: bool = True, siris: SirisAI = Depends(get_siris)) -> dict[str, Any]:
    """Turn "Away while I'm gone" on for the next trip (or off)."""
    from app.travel import service

    if not on:
        siris.state.set(service.AWAY_KEY, {})
        return {"away_mode": "as usual"}
    trips = service.find_trips(siris)
    if not trips:
        raise HTTPException(status_code=404, detail="No trip coming up in the calendar")
    return service.set_away(siris, trips[0])
