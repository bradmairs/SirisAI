"""Outcomes: what happened after SirisAI acted or suggested something.

Signals:
- accepted / dismissed: the user's answer to something in the attention feed
  (a habit suggestion, an approval, a memory proposal).
- ignored: an attention item left open for days.
- kept / undone: an action SirisAI took on its own that stayed, or that the
  user reversed soon after (a light turned straight back off).
- corrected: the user said "no, I meant..." right after a tool ran in chat.
- unverified: the tool ran but its own check afterwards said it didn't work.

Producers record here; `app/learning/feedback.py` reads it back to decide
what to keep offering and what to stop doing unasked.
"""

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.db.models import Outcome

SIGNALS = ("accepted", "dismissed", "ignored", "kept", "undone", "corrected", "unverified")
POSITIVE = ("accepted", "kept")
NEGATIVE = ("dismissed", "undone", "corrected")


def _naive(value: datetime) -> datetime:
    return value.astimezone(UTC).replace(tzinfo=None) if value.tzinfo else value


class OutcomeStore:
    def __init__(self, session_factory: sessionmaker) -> None:
        self._session_factory = session_factory

    def record(
        self,
        subject: str,
        signal: str,
        *,
        key: str | None = None,
        source: str = "",
        detail: dict[str, Any] | None = None,
        ref: str | None = None,
        user_id: str = "default",
        at: datetime | None = None,
    ) -> bool:
        """Store one outcome. Returns False when `ref` was already recorded."""
        if signal not in SIGNALS:
            raise ValueError(f"Unknown outcome signal '{signal}'")
        row = Outcome(
            subject=subject[:200],
            key=(key or subject)[:300],
            signal=signal,
            source=source[:60],
            detail=json.dumps(detail or {}, default=str),
            ref=ref,
            user_id=user_id,
        )
        if at is not None:
            row.created_at = _naive(at)
        with self._session_factory() as session:
            session.add(row)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                return False
        return True

    def seen(self, ref: str) -> bool:
        with self._session_factory() as session:
            return session.scalar(select(func.count(Outcome.id)).where(Outcome.ref == ref)) > 0

    def counts(self, *, subject: str | None = None, key: str | None = None, days: float = 60, now: datetime | None = None) -> dict[str, int]:
        cutoff = _naive((now or datetime.now(UTC)) - timedelta(days=days))
        stmt = select(Outcome.signal, func.count(Outcome.id)).where(Outcome.created_at >= cutoff).group_by(Outcome.signal)
        if subject is not None:
            stmt = stmt.where(Outcome.subject == subject)
        if key is not None:
            stmt = stmt.where(Outcome.key == key)
        with self._session_factory() as session:
            found = dict(session.execute(stmt).all())
        return {signal: int(found.get(signal, 0)) for signal in SIGNALS}

    def keys_with(self, signals: tuple[str, ...], days: float = 60, now: datetime | None = None) -> list[str]:
        cutoff = _naive((now or datetime.now(UTC)) - timedelta(days=days))
        with self._session_factory() as session:
            return list(
                session.scalars(
                    select(Outcome.key).where(Outcome.created_at >= cutoff, Outcome.signal.in_(signals)).distinct()
                ).all()
            )

    def summary(self, days: float = 30, now: datetime | None = None) -> list[dict[str, Any]]:
        """Per subject: counts of each signal and how often it went well."""
        cutoff = _naive((now or datetime.now(UTC)) - timedelta(days=days))
        with self._session_factory() as session:
            rows = session.execute(
                select(Outcome.subject, Outcome.signal, func.count(Outcome.id))
                .where(Outcome.created_at >= cutoff)
                .group_by(Outcome.subject, Outcome.signal)
            ).all()
        by_subject: dict[str, dict[str, int]] = {}
        for subject, signal, n in rows:
            by_subject.setdefault(subject, {s: 0 for s in SIGNALS})[signal] = int(n)
        out = []
        for subject, c in by_subject.items():
            good, bad = sum(c[s] for s in POSITIVE), sum(c[s] for s in NEGATIVE)
            out.append({"subject": subject, **c, "approval": round(good / (good + bad), 2) if good + bad else None})
        return sorted(out, key=lambda e: (e["approval"] is None, e["approval"] or 0))

    def recent(self, limit: int = 50, signal: str | None = None) -> list[dict[str, Any]]:
        stmt = select(Outcome).order_by(Outcome.id.desc()).limit(limit)
        if signal:
            stmt = stmt.where(Outcome.signal == signal)
        with self._session_factory() as session:
            return [
                {
                    "id": r.id,
                    "subject": r.subject,
                    "key": r.key,
                    "signal": r.signal,
                    "source": r.source,
                    "detail": json.loads(r.detail or "{}"),
                    "at": r.created_at.replace(tzinfo=UTC).isoformat(),
                }
                for r in session.scalars(stmt).all()
            ]

    def prune(self, older_than_days: float = 365, now: datetime | None = None) -> int:
        cutoff = _naive((now or datetime.now(UTC)) - timedelta(days=older_than_days))
        with self._session_factory() as session:
            rows = session.scalars(select(Outcome).where(Outcome.created_at < cutoff)).all()
            for row in rows:
                session.delete(row)
            session.commit()
            return len(rows)
