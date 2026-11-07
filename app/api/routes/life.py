"""Life admin (4.6): the document inbox, bills and subscriptions found in
email, and trips in the calendar. See app/documents, app/bills and
app/travel."""

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
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


# -- things you own (app/things, 5.0) -----------------------------------------


class ThingIn(BaseModel):
    name: str | None = None
    brand: str | None = None
    model: str | None = None
    room: str | None = None
    bought: str | None = None
    price: float | None = None
    shop: str | None = None
    warranty_months: int | None = None
    warranty_until: str | None = None
    manual: str | None = None
    notes: str | None = None


@router.get("/things")
def list_things(q: str = "", siris: SirisAI = Depends(get_siris)) -> list[dict[str, Any]]:
    from app.things import service

    return service.find(siris, q, limit=200)


@router.get("/email/status")
def email_status(siris: SirisAI = Depends(get_siris)) -> dict[str, Any]:
    from app.things import from_email

    return from_email.status(siris)


@router.post("/things/from-email")
async def things_from_email(days: int = Query(30, ge=1, le=1825), siris: SirisAI = Depends(get_siris)) -> dict[str, Any]:
    """Look through email for receipts and add what was bought to Things."""
    from app.things import from_email

    return await from_email.scan(siris, days=days)


@router.post("/things")
def add_thing(request: ThingIn, siris: SirisAI = Depends(get_siris)) -> dict[str, Any]:
    from app.things import service

    try:
        return service.add(siris, request.model_dump(exclude_none=True))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.put("/things/{thing_id}")
def update_thing(thing_id: int, request: ThingIn, siris: SirisAI = Depends(get_siris)) -> dict[str, Any]:
    from app.things import service

    try:
        return service.update(siris, thing_id, request.model_dump(exclude_unset=True))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="No such thing") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.delete("/things/{thing_id}")
def delete_thing(thing_id: int, siris: SirisAI = Depends(get_siris)) -> dict[str, Any]:
    from app.things import service

    try:
        service.delete(siris, thing_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="No such thing") from exc
    return {"deleted": thing_id}


# -- people (app/people, 5.0) -------------------------------------------------


class PersonIn(BaseModel):
    name: str | None = None
    relation: str | None = None
    birthday: str | None = None
    anniversary: str | None = None
    notes: str | None = None
    last_contact: str | None = None
    keep_in_touch_days: int | None = None


@router.get("/people")
def list_people(siris: SirisAI = Depends(get_siris)) -> list[dict[str, Any]]:
    """Everyone, soonest birthday or anniversary first."""
    from app.people import service

    return service.everyone(siris)


@router.post("/people")
def add_person(request: PersonIn, siris: SirisAI = Depends(get_siris)) -> dict[str, Any]:
    from app.people import service

    try:
        return service.add(siris, request.model_dump(exclude_none=True))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.put("/people/{person_id}")
def update_person(person_id: int, request: PersonIn, siris: SirisAI = Depends(get_siris)) -> dict[str, Any]:
    from app.people import service

    try:
        return service.update(siris, person_id, request.model_dump(exclude_unset=True))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="No such person") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.delete("/people/{person_id}")
def delete_person(person_id: int, siris: SirisAI = Depends(get_siris)) -> dict[str, Any]:
    from app.people import service

    try:
        service.delete(siris, person_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="No such person") from exc
    return {"deleted": person_id}


@router.get("/people/{person_id}/gifts")
def person_gifts(person_id: int, siris: SirisAI = Depends(get_siris)) -> dict[str, Any]:
    from app.people import service

    person = next((p for p in service.everyone(siris) if p["id"] == person_id), None)
    if person is None:
        raise HTTPException(status_code=404, detail="No such person")
    return {"ideas": service.gift_ideas(siris, person["name"]), "last_seen": service.last_seen(siris, person["name"])}


# -- news (app/news, 5.0) -----------------------------------------------------


@router.get("/news")
async def news(siris: SirisAI = Depends(get_siris)) -> dict[str, Any]:
    """Today's stories on your topics, new podcast episodes, and what came in overnight."""
    from app.news import service

    stories = await asyncio.to_thread(service.stories, siris)
    episodes = await asyncio.to_thread(service.episodes, siris)
    return {"stories": stories, "episodes": episodes, "overnight": service.overnight(siris)}
