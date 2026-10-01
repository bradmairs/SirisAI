"""Named JSON values that must survive a restart (the app_state table) --
guest mode, cursors and similar small facts that don't deserve a table each."""

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import sessionmaker

from app.db.models import AppState


class StateStore:
    def __init__(self, session_factory: sessionmaker) -> None:
        self._session_factory = session_factory

    def get(self, key: str, default: Any = None) -> Any:
        with self._session_factory() as session:
            row = session.get(AppState, key)
            return json.loads(row.value) if row is not None else default

    def set(self, key: str, value: Any) -> None:
        with self._session_factory() as session:
            row = session.get(AppState, key)
            if row is None:
                session.add(AppState(key=key, value=json.dumps(value)))
            else:
                row.value = json.dumps(value)
                row.updated_at = datetime.now(UTC)
            session.commit()

    def delete(self, key: str) -> None:
        with self._session_factory() as session:
            row = session.get(AppState, key)
            if row is not None:
                session.delete(row)
                session.commit()
