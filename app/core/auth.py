"""Bearer-token auth for /siris/* routes.

Deliberately minimal for a single-user homelab service: one shared secret
(`SIRISAI_API_KEY`), checked as `Authorization: Bearer <key>`. No users,
sessions, or scopes -- if that's ever needed (multiple people, third-party
integrations), replace this dependency without touching call sites.

6.68 (with the repository public):
- the key is compared in constant time;
- a push button's one-request token (app/core/action_tokens.py) is accepted
  in place of the key, for exactly the request it was made for;
- with no key set, only requests from this machine get in, unless
  SIRISAI_ALLOW_UNAUTHENTICATED=true says an open server is meant.
"""

import hmac

from fastapi import Depends, Header, HTTPException, Request

from app.core.config import Settings, get_settings

LOCAL_CLIENTS = {"127.0.0.1", "::1", "localhost", "testclient"}


async def require_api_key(
    request: Request,
    authorization: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> None:
    if not settings.api_key:
        host = request.client.host if request.client else ""
        if settings.allow_unauthenticated or host in LOCAL_CLIENTS:
            return
        raise HTTPException(status_code=401, detail="SIRISAI_API_KEY isn't set, so only this machine may connect.")

    if authorization is not None and hmac.compare_digest(authorization.encode(), f"Bearer {settings.api_key}".encode()):
        return
    from app.core import action_tokens

    token = request.headers.get(action_tokens.HEADER)
    if token and action_tokens.check(settings.api_key, token, request.method, request.url.path, await request.body()):
        return
    raise HTTPException(status_code=401, detail="Missing or invalid API key.")
