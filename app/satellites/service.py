"""Voice satellite room registry (milestone 48) -- so SirisAI can push a
reply to whichever room's satellite the user is currently in, rather
than always answering back through the device a request started on.

Two things this store answers: "what satellites exist and how do I
reach them" (register()/list_satellites()/get_by_room()) and "which
room is the user currently in" (current_room() -- whichever room had
the most recent activity, either a satellite registering/heartbeating
or a real voice interaction via record_activity()). Deliberately no
staleness cutoff: a lone satellite that registered once, days ago,
still counts as current. Refining that (a timeout, or folding in UniFi
presence) is future work this milestone doesn't try to guess at --
see the README's writeup for why.
"""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.db.models import SatelliteRegistration


class SatelliteStore:
    def __init__(self, session_factory: sessionmaker) -> None:
        self._session_factory = session_factory

    def register(self, room: str, callback_url: str) -> dict[str, Any]:
        with self._session_factory() as session:
            existing = session.scalar(
                select(SatelliteRegistration).where(SatelliteRegistration.room == room)
            )
            now = datetime.now(UTC)
            if existing is not None:
                existing.callback_url = callback_url
                existing.last_active_at = now
                session.commit()
                return self._serialize(existing)

            satellite = SatelliteRegistration(
                room=room, callback_url=callback_url, last_active_at=now
            )
            session.add(satellite)
            session.commit()
            session.refresh(satellite)
            return self._serialize(satellite)

    def list_satellites(self) -> list[dict[str, Any]]:
        with self._session_factory() as session:
            stmt = select(SatelliteRegistration).order_by(SatelliteRegistration.room)
            return [self._serialize(s) for s in session.scalars(stmt).all()]

    def get_by_room(self, room: str) -> dict[str, Any] | None:
        with self._session_factory() as session:
            satellite = session.scalar(
                select(SatelliteRegistration).where(SatelliteRegistration.room == room)
            )
            return self._serialize(satellite) if satellite is not None else None

    def record_activity(
        self, room: str, conversation_id: str | None = None, at: datetime | None = None
    ) -> None:
        """No-op if `room` isn't a registered satellite -- a chat request
        can name a room that never registered (or was later removed);
        that's not an error, just nothing to bump.

        `conversation_id` (milestone 75, conversation handoff) records
        which conversation was last in play from this room, so a later
        request from a *different* room with no conversation_id of its
        own can pick it up -- see handoff_candidate(). Omitted (rather
        than passed as None) leaves whatever was recorded before alone,
        so a caller that doesn't yet know the conversation_id (or isn't
        part of the handoff flow at all) can't accidentally blank it.

        `at` defaults to now -- only for backdating activity in tests
        that need to exercise handoff_candidate()'s staleness cutoff
        without actually sleeping, same pattern as
        EventMemoryStore.log_event()'s `created_at`."""
        with self._session_factory() as session:
            satellite = session.scalar(
                select(SatelliteRegistration).where(SatelliteRegistration.room == room)
            )
            if satellite is None:
                return
            satellite.last_active_at = at or datetime.now(UTC)
            if conversation_id is not None:
                satellite.last_conversation_id = conversation_id
            session.commit()

    def current_room(self) -> str | None:
        with self._session_factory() as session:
            stmt = (
                select(SatelliteRegistration.room)
                .order_by(SatelliteRegistration.last_active_at.desc())
                .limit(1)
            )
            return session.scalar(stmt)

    def handoff_candidate(self, exclude_room: str, within_seconds: float) -> str | None:
        """Conversation handoff between rooms (milestone 75): the
        conversation_id to resume when a fresh request comes in from
        `exclude_room` with no conversation_id of its own -- the most
        recently active *other* room's last conversation, but only if
        that activity is recent enough to plausibly be the same person
        having just walked from one room to the other rather than an
        old conversation resurfacing hours or days later."""
        with self._session_factory() as session:
            stmt = (
                select(SatelliteRegistration)
                .where(
                    SatelliteRegistration.room != exclude_room,
                    SatelliteRegistration.last_conversation_id.is_not(None),
                )
                .order_by(SatelliteRegistration.last_active_at.desc())
                .limit(1)
            )
            satellite = session.scalar(stmt)
            if satellite is None:
                return None
            last_active_at = satellite.last_active_at
            if last_active_at.tzinfo is None:
                # SQLite drops tzinfo on the way back out -- every stored
                # timestamp in this codebase is UTC by convention
                # (datetime.now(UTC) at write time), same pattern
                # self_maintenance.py's _older_than() already uses.
                last_active_at = last_active_at.replace(tzinfo=UTC)
            age = (datetime.now(UTC) - last_active_at).total_seconds()
            if age > within_seconds:
                return None
            return satellite.last_conversation_id

    @staticmethod
    def _serialize(satellite: SatelliteRegistration) -> dict[str, Any]:
        return {
            "id": satellite.id,
            "room": satellite.room,
            "callback_url": satellite.callback_url,
            "last_active_at": satellite.last_active_at.isoformat(),
        }
