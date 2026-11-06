"""Housekeeping: keep SirisAI from slowly filling its disk.

Once a day (SIRISAI_HOUSEKEEPING_HOUR_LOCAL, 5am) it:

- **prunes** what only matters for a while: the audit log after
  SIRISAI_AUDIT_KEEP_DAYS (180), metric samples after
  SIRISAI_METRICS_KEEP_DAYS (90), closed attention items after 60 days,
  delivered or cancelled reminders after 90, and learning outcomes after a
  year. Conversations and memories are never pruned. After a big prune it
  compacts the database (VACUUM), and every day it refreshes SQLite's query
  statistics and folds the write-ahead log back in;
- **measures** the database size, free disk space where the data lives, and
  LLM tokens used in the last day, keeping 60 days of history
  (`GET /siris/upkeep`);
- **warns** in the attention feed when free space drops under
  SIRISAI_DISK_WARN_PERCENT (10%, urgent and pushed under half that), when
  the day's tokens pass SIRISAI_TOKEN_BUDGET_DAILY (0 = no budget), when
  the database has doubled in a week, and when the newest backup is stale.
"""

import shutil
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select

from app.db.models import AttentionItem, MetricSample, Reminder, ToolExecution
from app.llm.metering import METRIC_TOTAL_TOKENS
from app.upkeep import alerts, backups

if TYPE_CHECKING:
    from app.core.siris import SirisAI

HISTORY_KEY = "housekeeping_history"
VACUUM_AFTER_ROWS = 5000


def _naive(value: datetime) -> datetime:
    return value.astimezone(UTC).replace(tzinfo=None)


def prune(siris: "SirisAI", now: datetime | None = None) -> dict[str, int]:
    now = now or datetime.now(UTC)
    s = siris.settings
    rules = [
        ("audit", ToolExecution, ToolExecution.created_at, s.audit_keep_days, None),
        ("metrics", MetricSample, MetricSample.created_at, s.metrics_keep_days, None),
        ("attention", AttentionItem, AttentionItem.updated_at, 60, AttentionItem.status != "open"),
        ("reminders", Reminder, Reminder.due_at, 90, Reminder.status.in_(("sent", "cancelled", "failed"))),
    ]
    pruned: dict[str, int] = {}
    session_factory = siris.audit._session_factory
    with session_factory() as session:
        for name, model, column, days, extra in rules:
            if not days or days <= 0:
                continue
            stmt = model.__table__.delete().where(column < _naive(now - timedelta(days=days)))
            if extra is not None:
                stmt = stmt.where(extra)
            pruned[name] = session.execute(stmt).rowcount or 0
        session.commit()
    pruned["outcomes"] = siris.outcomes.prune(365, now)
    path = backups.db_path(s)
    if path is not None and path.exists():
        conn = sqlite3.connect(path, isolation_level=None)  # VACUUM can't run inside a transaction
        try:
            if sum(pruned.values()) >= VACUUM_AFTER_ROWS:
                conn.execute("VACUUM")
                pruned["vacuumed"] = 1
            # Daily upkeep (4.6): refresh SQLite's statistics so it keeps
            # picking the right indexes as tables grow, and fold the
            # write-ahead log back into the database so it doesn't grow
            # without bound between restarts.
            conn.execute("PRAGMA optimize")
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            conn.close()
    return pruned


def tokens_since(siris: "SirisAI", since: datetime) -> int:
    with siris.metrics._session_factory() as session:
        total = session.scalar(
            select(func.sum(MetricSample.value)).where(MetricSample.metric_name == METRIC_TOTAL_TOKENS, MetricSample.created_at >= _naive(since))
        )
    return int(total or 0)


def measure(siris: "SirisAI", now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    path = backups.db_path(siris.settings)
    folder = backups.data_dir(siris.settings)
    out: dict[str, Any] = {"at": now.isoformat(), "tokens_24h": tokens_since(siris, now - timedelta(days=1))}
    if path is not None and path.exists():
        wal = path.with_name(path.name + "-wal")  # recent writes live here until a checkpoint
        out["db_bytes"] = path.stat().st_size + (wal.stat().st_size if wal.exists() else 0)
    try:
        usage = shutil.disk_usage(folder if folder.exists() else ".")
        out["disk_free_bytes"] = usage.free
        out["disk_free_percent"] = round(100 * usage.free / usage.total, 1)
    except OSError:
        pass
    return out


def run(siris: "SirisAI", now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    s = siris.settings
    pruned = prune(siris, now)
    reading = measure(siris, now)
    history = [*(siris.state.get(HISTORY_KEY) or []), reading][-60:]
    siris.state.set(HISTORY_KEY, history)

    warnings = []
    free = reading.get("disk_free_percent")
    if free is not None and free < s.disk_warn_percent:
        urgent = free < s.disk_warn_percent / 2
        gb = reading["disk_free_bytes"] / 1e9
        message = f"Only {free}% ({gb:.1f} GB) free on the disk SirisAI's data is on."
        warnings.append(message)
        alerts.raise_alert(siris, "upkeep:disk", "Disk nearly full", message, severity="urgent" if urgent else "attention", push=urgent)
    else:
        siris.attention.resolve_key("upkeep:disk", "Space freed")
    if s.token_budget_daily and reading["tokens_24h"] > s.token_budget_daily:
        message = f"{reading['tokens_24h']:,} LLM tokens in the last day, over the {s.token_budget_daily:,} budget (SIRISAI_TOKEN_BUDGET_DAILY)."
        warnings.append(message)
        alerts.raise_alert(siris, f"upkeep:tokens:{now.date().isoformat()}", "Over the token budget", message)
    week_ago = next((h for h in reversed(history[:-1]) if datetime.fromisoformat(h["at"]) <= now - timedelta(days=6)), None)
    if week_ago and week_ago.get("db_bytes") and reading.get("db_bytes", 0) > 2 * week_ago["db_bytes"] and reading["db_bytes"] > 50e6:
        message = f"SirisAI's database doubled in a week, to {reading['db_bytes'] / 1e6:.0f} MB."
        warnings.append(message)
        alerts.raise_alert(siris, "upkeep:db-growth", "Database growing fast", message, severity="info")
    stale = backups.freshness(siris, now)
    if stale:
        warnings.append(stale)
        alerts.raise_alert(siris, "upkeep:backup-stale", "Backups are out of date", stale + " Check the backup job (GET /siris/upkeep).")
    else:
        siris.attention.resolve_key("upkeep:backup-stale", "A fresh backup was made")
    return {"pruned": pruned, **reading, "warnings": warnings}

