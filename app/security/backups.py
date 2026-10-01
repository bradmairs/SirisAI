"""Did last night's backups actually work? Hyper Backup tasks from the
Synology NAS (SYNO.Backup.Task) judged by their last result *and* how long
ago their last successful run was -- a task that silently stopped running
looks "fine" by result alone."""

from datetime import datetime, timedelta, tzinfo
from typing import Any

_OK_RESULTS = {"done", "success", "succeeded", "ok"}
_RUNNING_STATES = {"backingup", "backing_up", "running", "waiting"}


def _parse_dsm_time(value: Any, zone: tzinfo) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    for fmt in ("%Y/%m/%d %H:%M", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(value.strip(), fmt).replace(tzinfo=zone)
        except ValueError:
            continue
    return None


def evaluate(tasks: list[dict[str, Any]], now: datetime, max_age_hours: float, zone: tzinfo) -> list[dict[str, Any]]:
    results = []
    for task in tasks:
        name = task.get("name") or f"task {task.get('task_id')}"
        result = str(task.get("last_bkp_result") or task.get("last_result") or "").lower()
        state = str(task.get("state") or task.get("status") or "").lower()
        last_ok = _parse_dsm_time(task.get("last_bkp_success_time"), zone)
        last_run = _parse_dsm_time(task.get("last_bkp_end_time") or task.get("last_bkp_time"), zone) or last_ok
        entry: dict[str, Any] = {
            "name": name,
            "last_result": result or None,
            "last_run": last_run.isoformat() if last_run else None,
            "ok": True,
            "problem": None,
        }
        reference = last_ok or (last_run if result in _OK_RESULTS else None)
        if state in _RUNNING_STATES:
            entry["problem"] = None
        elif result and result not in _OK_RESULTS:
            entry["ok"], entry["problem"] = False, f"last run {result}"
        elif reference is None:
            entry["ok"], entry["problem"] = False, "no successful run on record"
        elif now - reference > timedelta(hours=max_age_hours):
            hours = (now - reference).total_seconds() / 3600
            entry["ok"], entry["problem"] = False, f"last successful run was {hours:.0f} hours ago"
        results.append(entry)
    return results
