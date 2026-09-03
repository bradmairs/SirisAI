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
            existing = session.scalar(select(SatelliteRegistration).where(SatelliteRegistration.room == room))
            now = datetime.now(UTC)
            if existing is not None:
                existing.callback_url = callback_url
                existing.last_active_at = now
                session.commit()
                return self._serialize(existing)

            satellite = SatelliteRegistration(room=room, callback_url=callback_url, last_active_at=now)
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
            satellite = session.scalar(select(SatelliteRegistration).where(SatelliteRegistration.room == room))
            return self._serialize(satellite) if satellite is not None else None

    def record_activity(self, room: str) -> None:
        """No-op if `room` isn't a registered satellite -- a chat request
        can name a room that never registered (or was later removed);
        that's not an error, just nothing to bump."""
        with self._session_factory() as session:
            satellite = session.scalar(select(SatelliteRegistration).where(SatelliteRegistration.room == room))
            if satellite is None:
                return
            satellite.last_active_at = datetime.now(UTC)
            session.commit()

    def current_room(self) -> str | None:
        with self._session_factory() as session:
            stmt = select(SatelliteRegistration.room).order_by(SatelliteRegistration.last_active_at.desc()).limit(1)
            return session.scalar(stmt)

    @staticmethod
    def _serialize(satellite: SatelliteRegistration) -> dict[str, Any]:
        return {
            "id": satellite.id,
            "room": satellite.room,
            "callback_url": satellite.callback_url,
            "last_active_at": satellite.last_active_at.isoformat(),
        }
