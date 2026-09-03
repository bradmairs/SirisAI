"""SQLite engine/session setup.

SQLite + SQLAlchemy needs `check_same_thread=False` because FastAPI can hand
requests to different threads than the one that opened the connection --
and, since a performance hardening pass, tool calls and context-building
genuinely do run on worker threads now (`asyncio.to_thread()` in
app/cortex/orchestrator.py/app/planning/planner.py, so a slow tool call
doesn't block this process's single event loop), not just conceptually.

WAL mode (below) is what makes that safe to do without lock contention:
SQLite's default rollback-journal mode serializes every writer against
every reader (and vice versa) -- fine when only one thread ever touches
the database at a time, a real problem once concurrent tool calls and
audit-log writes are actually happening across threads, which is exactly
what the asyncio.to_thread() change above makes routine. WAL lets readers
and a writer proceed concurrently instead.
"""

from collections.abc import Generator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings

settings = get_settings()

engine = create_engine(
    settings.database_url,
    connect_args={"check_same_thread": False} if settings.database_url.startswith("sqlite") else {},
)

if settings.database_url.startswith("sqlite"):

    @event.listens_for(engine, "connect")
    def _configure_sqlite_pragmas(dbapi_connection: object, connection_record: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA journal_mode=WAL")
        # NORMAL (rather than SQLite's default FULL) skips an fsync on
        # every commit -- WAL mode's own documented safe pairing: still
        # durable against this process crashing, only risks losing the
        # last few commits if the OS itself crashes or loses power. An
        # acceptable, deliberate tradeoff for a homelab personal
        # assistant's audit/memory/goal data, not a financial ledger.
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()


SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency that yields a DB session and always closes it."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
