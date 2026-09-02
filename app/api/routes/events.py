"""The event bus: an inbound webhook so external systems (Docker, Home
Assistant, UniFi, Plex, calendar, ...) can push events into SirisAI
instead of it only discovering things by being asked or polling through
tools.

Deliberately does nothing beyond logging to event memory
(app/memory/events.py) for now -- deciding whether to ignore, investigate,
or proactively notify based on an incoming event is a later milestone.
Logged events are picked up automatically by the context engine
(app/context/engine.py) and the recall_events tool, so they're already
useful without any of that.
"""

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.core.auth import require_api_key
from app.core.siris import SirisAI, get_siris

router = APIRouter(prefix="/siris/events", tags=["events"], dependencies=[Depends(require_api_key)])


class EventRequest(BaseModel):
    source: str
    description: str
    entity_name: str | None = None
    data: dict[str, Any] | None = None


class EventResponse(BaseModel):
    id: int
    entity_name: str | None
    description: str
    source: str | None
    data: dict[str, Any] | None
    created_at: str


@router.post("", response_model=EventResponse)
def receive_event(request: EventRequest, siris: SirisAI = Depends(get_siris)) -> EventResponse:
    event = siris.events.log_event(
        request.description, entity_name=request.entity_name, source=request.source, data=request.data
    )
    return EventResponse(**event)


@router.get("", response_model=list[EventResponse])
def list_events(
    entity_name: str | None = None, limit: int = 50, siris: SirisAI = Depends(get_siris)
) -> list[EventResponse]:
    return [EventResponse(**e) for e in siris.events.recent_events(entity_name=entity_name, limit=limit)]
