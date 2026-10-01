"""Energy-aware scheduling: hold heavy jobs (a backup, a big download, the
dishwasher) until power is cheap or the sun is doing the work.

"Cheap right now" is true when any configured signal says so:
- inside a cheap tariff window (SIRISAI_ENERGY_CHEAP_WINDOWS, local time,
  e.g. "00:00-06:00,11:00-14:00"; windows may cross midnight);
- solar production (SIRISAI_ENERGY_SOLAR_ENTITY_ID, a Home Assistant power
  sensor in W or kW) at or above SIRISAI_ENERGY_SOLAR_MIN_WATTS;
- the live price (SIRISAI_ENERGY_PRICE_ENTITY_ID) at or below
  SIRISAI_ENERGY_PRICE_MAX.

A deferred job is any tool call. Scheduling one needs the same approval the
tool itself needs (confirming "run the dishwasher when power's cheap" *is*
confirming the dishwasher), so the job later runs with that approval
attached. The scheduler checks every tick and runs waiting jobs when it's
cheap -- or at a job's deadline regardless, so nothing waits forever on a
cloudy week.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, time, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.core import clock
from app.db.models import DeferredJob

if TYPE_CHECKING:
    from app.core.siris import SirisAI


def parse_windows(spec: str) -> list[tuple[time, time]]:
    windows = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        start, _, end = part.partition("-")
        windows.append((time.fromisoformat(start.strip()), time.fromisoformat(end.strip())))
    return windows


def _in_window(now: time, window: tuple[time, time]) -> bool:
    start, end = window
    return start <= now < end if start <= end else now >= start or now < end


def _sensor_value(siris: "SirisAI", entity_id: str) -> float | None:
    client = siris._home_assistant_client
    if not entity_id or client is None:
        return None
    state = client.get_state_or_none(entity_id) or {}
    try:
        value = float(state.get("state"))
    except (TypeError, ValueError):
        return None
    unit = str((state.get("attributes") or {}).get("unit_of_measurement", "")).lower()
    return value * 1000 if unit == "kw" else value


def status(siris: "SirisAI", now: datetime | None = None) -> dict[str, Any]:
    settings = siris.settings
    now = now or datetime.now(UTC)
    local = now.astimezone(clock.get_zone(settings.timezone))
    windows = parse_windows(settings.energy_cheap_windows)
    reasons: list[str] = []
    in_window = next((w for w in windows if _in_window(local.time(), w)), None)
    if in_window:
        reasons.append(f"inside the cheap window {in_window[0]:%H:%M}-{in_window[1]:%H:%M}")
    solar = _sensor_value(siris, settings.energy_solar_entity_id)
    if solar is not None and solar >= settings.energy_solar_min_watts:
        reasons.append(f"solar is producing {solar:.0f} W")
    price = _sensor_value(siris, settings.energy_price_entity_id)
    if price is not None and settings.energy_price_max is not None and price <= settings.energy_price_max:
        reasons.append(f"the price is {price:g} (at or under {settings.energy_price_max:g})")

    next_window = None
    if windows and not in_window:
        starts = []
        for start, _end in windows:
            candidate = local.replace(hour=start.hour, minute=start.minute, second=0, microsecond=0)
            if candidate <= local:
                candidate += timedelta(days=1)
            starts.append(candidate)
        next_window = min(starts).isoformat()
    return {
        "configured": bool(windows or settings.energy_solar_entity_id or settings.energy_price_entity_id),
        "cheap_now": bool(reasons),
        "reasons": reasons,
        "solar_watts": solar,
        "price": price,
        "next_cheap_window": next_window,
    }


class DeferredJobStore:
    def __init__(self, session_factory: sessionmaker) -> None:
        self._session_factory = session_factory

    def add(self, tool_name: str, arguments: dict[str, Any], reason: str, confirmed: bool, deadline: datetime | None) -> dict[str, Any]:
        with self._session_factory() as session:
            job = DeferredJob(
                tool_name=tool_name,
                arguments=json.dumps(arguments),
                reason=reason,
                confirmed=confirmed,
                deadline=deadline.astimezone(UTC).replace(tzinfo=None) if deadline else None,
            )
            session.add(job)
            session.commit()
            session.refresh(job)
            return self._serialize(job)

    def list(self, status: str | None = "waiting") -> list[dict[str, Any]]:
        with self._session_factory() as session:
            stmt = select(DeferredJob).order_by(DeferredJob.id)
            if status:
                stmt = stmt.where(DeferredJob.status == status)
            return [self._serialize(j) for j in session.scalars(stmt).all()]

    def finish(self, job_id: int, status: str, outcome: str) -> None:
        with self._session_factory() as session:
            job = session.get(DeferredJob, job_id)
            if job is not None:
                job.status, job.outcome, job.ran_at = status, outcome[:2000], datetime.now(UTC).replace(tzinfo=None)
                session.commit()

    def cancel(self, job_id: int) -> bool:
        with self._session_factory() as session:
            job = session.get(DeferredJob, job_id)
            if job is None or job.status != "waiting":
                return False
            job.status = "cancelled"
            session.commit()
            return True

    @staticmethod
    def _serialize(job: DeferredJob) -> dict[str, Any]:
        return {
            "id": job.id,
            "tool_name": job.tool_name,
            "arguments": json.loads(job.arguments),
            "reason": job.reason,
            "status": job.status,
            "deadline": job.deadline.replace(tzinfo=UTC).isoformat() if job.deadline else None,
            "created_at": job.created_at.isoformat(),
            "ran_at": job.ran_at.isoformat() if job.ran_at else None,
            "outcome": job.outcome,
            "confirmed": job.confirmed,
        }


def run_due(siris: "SirisAI", now: datetime | None = None) -> list[dict[str, Any]]:
    """Run waiting jobs if power is cheap now, or any whose deadline passed."""
    now = now or datetime.now(UTC)
    jobs = siris.deferred_jobs.list("waiting")
    if not jobs:
        return []
    energy = status(siris, now)
    ran = []
    for job in jobs:
        overdue = job["deadline"] is not None and datetime.fromisoformat(job["deadline"]) <= now
        if not (energy["cheap_now"] or overdue):
            continue
        why = "deadline reached" if overdue and not energy["cheap_now"] else "; ".join(energy["reasons"])
        try:
            result = siris.run_tool(
                job["tool_name"],
                job["arguments"],
                confirmed=job["confirmed"],
                trigger=f"energy scheduler: deferred job #{job['id']} ({job['reason'] or job['tool_name']}) -- {why}",
            )
            siris.deferred_jobs.finish(job["id"], "done", json.dumps(result, default=str))
            message = f"Ran {job['reason'] or job['tool_name']} while power was cheap ({why})."
            ran.append({"id": job["id"], "status": "done", "why": why})
        except Exception as exc:
            siris.deferred_jobs.finish(job["id"], "failed", str(exc))
            message = f"Deferred job '{job['reason'] or job['tool_name']}' failed: {exc}"[:300]
            ran.append({"id": job["id"], "status": "failed", "error": str(exc)[:200]})
        siris.pending_notifications.queue(message, severity="info", category="energy")
    return ran
