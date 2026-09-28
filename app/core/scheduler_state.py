"""SchedulerStateStore: the single persisted fact app/core/scheduler.py
needs across restarts -- which calendar date the daily briefing last went
out on. See app/db/models.py's SchedulerState for why this needs a real
row instead of an in-memory timer, unlike the scheduler's other,
interval-based checks."""

from sqlalchemy.orm import sessionmaker

from app.db.models import SchedulerState

_STATE_ID = 1


class SchedulerStateStore:
    def __init__(self, session_factory: sessionmaker) -> None:
        self._session_factory = session_factory

    def last_briefing_date(self) -> str | None:
        with self._session_factory() as session:
            row = session.get(SchedulerState, _STATE_ID)
            return row.last_briefing_date if row is not None else None

    def mark_briefing_sent(self, date_str: str) -> None:
        with self._session_factory() as session:
            row = session.get(SchedulerState, _STATE_ID)
            if row is None:
                session.add(SchedulerState(id=_STATE_ID, last_briefing_date=date_str))
            else:
                row.last_briefing_date = date_str
            session.commit()
