"""Discovered-integration persistence (milestone 32). Same "own
short-lived session per call" pattern as GoalStore/RoutineStore.
"""

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.db.models import Integration


class IntegrationStore:
    def __init__(self, session_factory: sessionmaker) -> None:
        self._session_factory = session_factory

    def save_integration(
        self,
        name: str,
        base_url: str,
        spec: dict[str, Any],
        auth_header_name: str | None = None,
        auth_header_value: str | None = None,
    ) -> dict[str, Any]:
        with self._session_factory() as session:
            row = Integration(
                name=name,
                base_url=base_url,
                spec=json.dumps(spec),
                auth_header_name=auth_header_name,
                auth_header_value=auth_header_value,
            )
            session.add(row)
            session.commit()
            return self._serialize(row, include_secrets=False)

    def list_integrations(self, include_secrets: bool = False) -> list[dict[str, Any]]:
        """`include_secrets=True` also returns `auth_header_value` --
        internal-only, used by SirisAI to actually build authenticated
        HTTP clients when re-registering tools at startup. Never set it
        from a route or tool -- the public shape only ever reports
        `has_auth`."""
        with self._session_factory() as session:
            stmt = select(Integration).order_by(Integration.created_at.desc())
            return [self._serialize(row, include_secrets=include_secrets) for row in session.scalars(stmt).all()]

    @staticmethod
    def _serialize(row: Integration, include_secrets: bool) -> dict[str, Any]:
        result: dict[str, Any] = {
            "id": row.id,
            "name": row.name,
            "base_url": row.base_url,
            "spec": json.loads(row.spec),
            "auth_header_name": row.auth_header_name,
            "has_auth": row.auth_header_value is not None,
            "created_at": row.created_at.isoformat(),
        }
        if include_secrets:
            result["auth_header_value"] = row.auth_header_value
        return result
