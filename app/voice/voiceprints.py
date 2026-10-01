"""Enrolled voiceprints and speaker identification (see app/voice/speaker.py
for the embedding itself).

A person's voiceprint is the running mean of every sample they've enrolled,
so a few short recordings in different rooms/moods make it sturdier.
Identification picks the closest voiceprint, but only when it's both similar
enough (SIRISAI_SPEAKER_ID_THRESHOLD) and clearly closer than the runner-up
-- two similar-sounding people shouldn't get confused for each other on a
coin flip; an uncertain match falls back to the default user instead.
"""

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.db.models import Voiceprint
from app.voice.speaker import cosine

_MIN_MARGIN = 0.02


class VoiceprintStore:
    def __init__(self, session_factory: sessionmaker) -> None:
        self._session_factory = session_factory

    def enroll(self, user_id: str, embedding: list[float]) -> dict[str, Any]:
        with self._session_factory() as session:
            row = session.scalars(select(Voiceprint).where(Voiceprint.user_id == user_id)).first()
            if row is None:
                row = Voiceprint(user_id=user_id, embedding=json.dumps(embedding), sample_count=1)
                session.add(row)
            else:
                current = json.loads(row.embedding)
                n = row.sample_count
                row.embedding = json.dumps([(c * n + e) / (n + 1) for c, e in zip(current, embedding, strict=True)])
                row.sample_count = n + 1
                row.updated_at = datetime.now(UTC)
            session.commit()
            return {"user_id": user_id, "samples": row.sample_count}

    def list_voiceprints(self) -> list[dict[str, Any]]:
        with self._session_factory() as session:
            rows = session.scalars(select(Voiceprint).order_by(Voiceprint.user_id)).all()
            return [
                {"user_id": r.user_id, "samples": r.sample_count, "updated_at": r.updated_at.isoformat()}
                for r in rows
            ]

    def delete(self, user_id: str) -> bool:
        with self._session_factory() as session:
            row = session.scalars(select(Voiceprint).where(Voiceprint.user_id == user_id)).first()
            if row is None:
                return False
            session.delete(row)
            session.commit()
            return True

    def identify(self, embedding: list[float], threshold: float) -> dict[str, Any]:
        """{"user_id": best match or None, "score": its similarity, "scores":
        every enrolled user's similarity}."""
        with self._session_factory() as session:
            rows = session.scalars(select(Voiceprint)).all()
            prints = {r.user_id: json.loads(r.embedding) for r in rows}
        # A print from a different encoder (another dimension) can't be compared.
        scores = {user: round(cosine(embedding, vec), 4) for user, vec in prints.items() if len(vec) == len(embedding)}
        ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
        if not ranked:
            return {"user_id": None, "score": None, "scores": {}}
        best_user, best = ranked[0]
        runner_up = ranked[1][1] if len(ranked) > 1 else -1.0
        matched = best >= threshold and best - runner_up >= _MIN_MARGIN
        return {"user_id": best_user if matched else None, "score": best, "scores": scores}
