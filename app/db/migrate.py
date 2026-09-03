"""Hand-rolled patch for columns added to tables that already existed on
disk (milestone 46's `user_id` was the first case of this in the codebase).

`Base.metadata.create_all()` (app/main.py) creates missing *tables* fine,
but does not add missing *columns* to a table that already exists --
so a SQLite file predating one of these milestones needs the column added
by hand, or every query touching it breaks outright (confirmed live: a
dev DB from before milestone 28/the verification milestone hit
`OperationalError: table tool_executions has no column named trigger` on
every single tool call). Not Alembic (app/db/base.py's docstring explains
why not yet): this only has to cover the exact columns this codebase has
actually added after their table already shipped -- a short, explicit
list, not a generic schema-diffing engine. Extend the list every time a
new one comes along -- it's silent and easy to miss since a brand-new DB
(most CI runs, most fresh installs) never exercises the gap at all.
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
]


def apply_column_patches(engine: Engine) -> None:
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    with engine.begin() as conn:
        for table_name, column_name, ddl_type in _ADDED_COLUMNS:
            if table_name not in existing_tables:
                continue  # a brand-new DB -- create_all() already made it right
            existing_columns = {col["name"] for col in inspector.get_columns(table_name)}
            if column_name in existing_columns:
                continue
            conn.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {ddl_type}"))
