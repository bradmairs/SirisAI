"""Known network device registry (a hardening pass): which Home Assistant
device_tracker entities NewDeviceAlertSkill has already seen, so it can
tell "already known" from "just appeared." Global, not per-user (milestone
46) -- same reasoning as SatelliteStore: which devices are on the
household network is physical infrastructure, not conversation content
scoped to whoever's chatting.
"""

from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.db.models import KnownNetworkDevice


class KnownDeviceStore:
    def __init__(self, session_factory: sessionmaker) -> None:
        self._session_factory = session_factory

    def list_known(self) -> set[str]:
        with self._session_factory() as session:
            return set(session.scalars(select(KnownNetworkDevice.entity_id)).all())

    def mark_known(self, entity_ids: Iterable[str]) -> None:
        """Adds every id in `entity_ids` not already known. Callers
        (NewDeviceAlertSkill) always compute `entity_ids` from a fresh
        `list_known()` in the same check, so a duplicate insert here
        would only happen if two checks ran concurrently against the
        same new device -- realistically as unlikely as, and accepted
        the same way as, SatelliteStore.register()'s own non-atomic
        read-then-write."""
        ids = list(entity_ids)
        if not ids:
            return
        with self._session_factory() as session:
            session.add_all([KnownNetworkDevice(entity_id=entity_id) for entity_id in ids])
            session.commit()
