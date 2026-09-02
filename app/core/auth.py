"""Bearer-token auth for /siris/* routes.

Deliberately minimal for a single-user homelab service: one shared secret
(`SIRISAI_API_KEY`), checked as `Authorization: Bearer <key>`. No users,
sessions, or scopes -- if that's ever needed (multiple people, third-party
integrations), replace this dependency without touching call sites.
"""

from fastapi import Depends, Header, HTTPException

from app.core.config import Settings, get_settings


async def require_api_key(
    authorization: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> None:
    if not settings.api_key:
        # No key configured -- auth is off (local dev default).
        return

    if authorization != f"Bearer {settings.api_key}":
        raise HTTPException(status_code=401, detail="Missing or invalid API key.")
