"""Backups you can trust: a nightly copy of SirisAI's database, and a monthly
restore drill that proves the copies actually open.

- **Backup** (SIRISAI_BACKUP_HOUR_LOCAL, 2am): SQLite's online backup API
  copies the live database consistently while SirisAI keeps running, and
  the copy is gzipped into SIRISAI_BACKUP_DIR (default `backups/` next to
  the database). The newest SIRISAI_BACKUP_KEEP (14) are kept. With
  SIRISAI_BACKUP_COPY_TO set (a NAS mount, say), each one is copied there
  too, so a dead disk doesn't take the backups with it.
- **Restore drill** (SIRISAI_RESTORE_DRILL_HOUR_LOCAL on the 1st of the
  month, 4am): the newest backup is unpacked into a scratch file, checked
  with `PRAGMA integrity_check`, compared table by table with the live
  database, and read through SirisAI's own stores. A pass is a quiet note
  in the attention feed; a failure is urgent and pushed.
- **Freshness**: the housekeeping job warns when the newest backup is more
  than two days old.

Only for SQLite (the default); with another database these report that
they were skipped.
"""

import gzip
import shutil
import sqlite3
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.upkeep import alerts

if TYPE_CHECKING:
    from app.core.config import Settings
    from app.core.siris import SirisAI

PREFIX = "sirisai-"
SUFFIX = ".db.gz"
STALE_AFTER = timedelta(days=2)


def db_path(settings: "Settings") -> Path | None:
    url = settings.database_url
    if not url.startswith("sqlite:///"):
        return None
    return Path(url.removeprefix("sqlite:///"))


def data_dir(settings: "Settings") -> Path:
    path = db_path(settings)
    return path.parent if path is not None else Path("./data")


def backup_dir(settings: "Settings") -> Path:
    return Path(settings.backup_dir) if settings.backup_dir else data_dir(settings) / "backups"


def list_backups(settings: "Settings") -> list[dict[str, Any]]:
    folder = backup_dir(settings)
    if not folder.is_dir():
        return []
    files = sorted(folder.glob(f"{PREFIX}*{SUFFIX}"), reverse=True)
    return [
        {"name": f.name, "path": str(f), "size_bytes": f.stat().st_size, "at": datetime.fromtimestamp(f.stat().st_mtime, UTC).isoformat()}
        for f in files
    ]


def make_backup(siris: "SirisAI", now: datetime | None = None) -> dict[str, Any]:
    settings = siris.settings
    source = db_path(settings)
    if source is None:
        return {"skipped": "not a SQLite database"}
    if not source.exists():
        return {"skipped": f"{source} doesn't exist yet"}
    now = now or datetime.now(UTC)
    folder = backup_dir(settings)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{PREFIX}{now.strftime('%Y%m%d-%H%M%S')}{SUFFIX}"
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "copy.db"
        src, dst = sqlite3.connect(source), sqlite3.connect(copy)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        with open(copy, "rb") as raw, gzip.open(target, "wb", compresslevel=6) as packed:
            shutil.copyfileobj(raw, packed)
    removed = []
    for old in list_backups(settings)[max(settings.backup_keep, 1) :]:
        Path(old["path"]).unlink(missing_ok=True)
        removed.append(old["name"])
    result: dict[str, Any] = {"path": str(target), "size_bytes": target.stat().st_size, "removed": removed}
    if settings.backup_copy_to:
        try:
            off_box = Path(settings.backup_copy_to)
            off_box.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, off_box / target.name)
            for old in sorted(off_box.glob(f"{PREFIX}*{SUFFIX}"), reverse=True)[max(settings.backup_keep, 1) :]:
                old.unlink(missing_ok=True)
            result["copied_to"] = str(off_box / target.name)
        except OSError as exc:
            result["copy_error"] = str(exc)
            alerts.raise_alert(siris, "upkeep:backup-copy", "Backup copy failed", f"Couldn't copy tonight's backup to {settings.backup_copy_to}: {exc}", severity="attention")
    return result


def _tables(conn: sqlite3.Connection) -> dict[str, int]:
    names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    return {name: conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0] for name in names}


def restore_drill(siris: "SirisAI", now: datetime | None = None, notify: bool = True) -> dict[str, Any]:
    """Unpack the newest backup and prove it's usable."""
    settings = siris.settings
    live = db_path(settings)
    if live is None:
        return {"skipped": "not a SQLite database"}
    backups = list_backups(settings)
    problems: list[str] = []
    report: dict[str, Any] = {"backup": backups[0]["name"] if backups else None}
    if not backups:
        problems.append("there are no backups to restore")
    else:
        with tempfile.TemporaryDirectory() as tmp:
            restored = Path(tmp) / "restored.db"
            try:
                with gzip.open(backups[0]["path"], "rb") as packed, open(restored, "wb") as raw:
                    shutil.copyfileobj(packed, raw)
                conn = sqlite3.connect(restored)
                try:
                    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
                    restored_tables = _tables(conn)
                finally:
                    conn.close()
            except (OSError, EOFError, sqlite3.DatabaseError) as exc:
                problems.append(f"the newest backup won't open: {exc}")
                integrity, restored_tables = "unreadable", {}
            report["integrity"] = integrity
            if integrity != "ok" and not problems:
                problems.append(f"integrity check failed: {integrity}")
            if restored_tables:
                live_conn = sqlite3.connect(live)
                try:
                    live_tables = _tables(live_conn)
                finally:
                    live_conn.close()
                missing = sorted(set(live_tables) - set(restored_tables))
                if missing:
                    problems.append(f"tables missing from the backup: {', '.join(missing)}")
                report["tables"] = len(restored_tables)
                report["rows"] = {k: restored_tables[k] for k in ("memories", "goals", "conversations", "tool_executions") if k in restored_tables}
                report["live_rows"] = {k: live_tables[k] for k in report["rows"] if k in live_tables}
                try:
                    report["memories_readable"] = _read_through_stores(restored)
                except Exception as exc:  # noqa: BLE001 - any failure here is a failed drill
                    problems.append(f"SirisAI couldn't read the restored copy: {type(exc).__name__}: {exc}")
    report["passed"] = not problems
    report["problems"] = problems
    if notify:
        if problems:
            alerts.raise_alert(
                siris, "upkeep:restore-drill", "Restore drill failed", "The monthly restore drill failed: " + "; ".join(problems) + ".", severity="urgent", push=True, issue=True
            )
        else:
            rows = report.get("rows", {})
            alerts.raise_alert(
                siris,
                "upkeep:restore-drill",
                "Restore drill passed",
                f"{report['backup']} unpacked and checked out: {report['tables']} tables, {rows.get('memories', 0)} memories, {rows.get('conversations', 0)} conversations.",
                severity="info",
            )
    return report


def _read_through_stores(path: Path) -> int:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.memory.service import MemoryStore

    engine = create_engine(f"sqlite:///{path}")
    try:
        with sessionmaker(bind=engine)() as session:
            from app.db.models import MemoryEntry

            session.query(MemoryEntry).first()
        return len(MemoryStore(sessionmaker(bind=engine)).list_all())
    finally:
        engine.dispose()


def freshness(siris: "SirisAI", now: datetime | None = None) -> str | None:
    """A warning when the newest backup is too old, else None."""
    if db_path(siris.settings) is None or siris.settings.backup_hour_local is None:
        return None
    now = now or datetime.now(UTC)
    backups = list_backups(siris.settings)
    if not backups:
        return "There are no backups of SirisAI's database yet."
    age = now - datetime.fromisoformat(backups[0]["at"])
    if age > STALE_AFTER:
        return f"The newest backup of SirisAI's database is {age.days} days old."
    return None
