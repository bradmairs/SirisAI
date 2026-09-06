"""Hand-rolled patches for schema changes made to tables that already
existed on disk (milestone 46's `user_id` was the first case of this in
the codebase; a performance hardening pass added the index-patching
half below).

`Base.metadata.create_all()` (app/main.py) creates missing *tables* fine,
but does not add missing *columns* or *indexes* to a table that already
exists -- so a SQLite file predating one of these milestones needs the
column/index added by hand, or (for a missing column) every query
touching it breaks outright (confirmed live: a dev DB from before
milestone 28/the verification milestone hit `OperationalError: table
tool_executions has no column named trigger` on every single tool call);
a missing index just means an existing deployment silently never gets
the speedup, no error, so it's worth patching in too rather than only
benefiting a fresh install. Not Alembic (app/db/base.py's docstring
explains why not yet): each patch list only has to cover the exact
columns/indexes this codebase has actually added after their table
already shipped -- short, explicit lists, not a generic schema-diffing
engine. Extend them every time a new one comes along -- it's silent and
easy to miss since a brand-new DB (most CI runs, most fresh installs)
never exercises the gap at all.
"""

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

# (table, column, SQLite column DDL including a default so existing rows
# backfill cleanly instead of erroring on the NOT NULL added below).
_ADDED_COLUMNS = [
    ("memories", "user_id", "TEXT NOT NULL DEFAULT 'default'"),
    ("goals", "user_id", "TEXT NOT NULL DEFAULT 'default'"),
    # Self-maintenance / reflection (milestone 30).
    ("memories", "recall_count", "INTEGER NOT NULL DEFAULT 0"),
    ("memories", "last_recalled_at", "DATETIME"),
    # Explainable autonomy (milestone 28).
    ("tool_executions", "trigger", "TEXT"),
    ("tool_executions", "evidence", "TEXT"),
    ("tool_executions", "policy", "TEXT"),
    # Deterministic per-tool verification.
    ("tool_executions", "verified", "BOOLEAN"),
    ("tool_executions", "verification_detail", "TEXT"),
    # Conversation handoff between rooms (milestone 75).
    ("satellite_registrations", "last_conversation_id", "TEXT"),
    # Semantic memory recall.
    ("memories", "embedding", "TEXT"),
]


def apply_column_patches(engine: Engine) -> None:
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    with engine.begin() as conn:
        for table_name, column_name, ddl_type in _ADDED_COLUMNS:
            if table_name not in existing_tables:
                continue  # a brand-new DB -- create_all() already made it right
            existing_columns = {
                col["name"] for col in inspector.get_columns(table_name)
            }
            if column_name in existing_columns:
                continue
            conn.execute(
                text(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {ddl_type}")
            )


# (table, index name, column) -- same reasoning as _ADDED_COLUMNS above,
# for an index added (a performance hardening pass) to a column on a
# table that already existed without one. Index names match SQLAlchemy's
# own default naming convention (ix_<table>_<column>) exactly, so
# CREATE INDEX IF NOT EXISTS is a true no-op on a fresh install -- there,
# create_all() already created the same index under the same name when
# the table was first created, since index=True is set directly on
# these columns' model definitions now (see app/db/models.py).
_ADDED_INDEXES = [
    ("memories", "ix_memories_created_at", "created_at"),
    ("tool_executions", "ix_tool_executions_created_at", "created_at"),
    ("events", "ix_events_created_at", "created_at"),
    ("goals", "ix_goals_created_at", "created_at"),
]


def apply_index_patches(engine: Engine) -> None:
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    with engine.begin() as conn:
        for table_name, index_name, column_name in _ADDED_INDEXES:
            if table_name not in existing_tables:
                continue
            conn.execute(
                text(
                    f"CREATE INDEX IF NOT EXISTS {index_name} ON {table_name} ({column_name})"
                )
            )
