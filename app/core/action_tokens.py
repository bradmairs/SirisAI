"""One-tap action tokens for push buttons (6.68).

A push's Approve, Decline or Confirm button posts back to SirisAI straight
from the phone, so it has to carry something that lets it through. It used
to carry the master key (`Authorization: Bearer <SIRISAI_API_KEY>`), and
pushes travel through ntfy (ntfy.sh by default), where anyone who knows the
topic can read every message, buttons included. With the repository public,
how to use that key is no secret either.

Now each button carries `X-Siris-Action: <expiry>.<signature>`: an HMAC
over that exact request (method, path and body) and an expiry, signed with
a key derived from SIRISAI_API_KEY. It's good for that one button for
TOKEN_HOURS (24), and nothing else: not another item, another action,
other arguments, or any other route. require_api_key accepts it in place
of the key.
"""

from __future__ import annotations

import hashlib
import hmac
import time

TOKEN_HOURS = 24
HEADER = "X-Siris-Action"


def _secret(api_key: str) -> bytes:
    return hmac.new(api_key.encode(), b"sirisai push action tokens v1", hashlib.sha256).digest()


def _sign(api_key: str, method: str, path: str, body: bytes, expires: int) -> str:
    message = b"\n".join([method.upper().encode(), path.encode(), hashlib.sha256(body).hexdigest().encode(), str(expires).encode()])
    return hmac.new(_secret(api_key), message, hashlib.sha256).hexdigest()


def make(api_key: str, method: str, path: str, body: str | bytes, hours: float = TOKEN_HOURS, now: float | None = None) -> str:
    expires = int((now or time.time()) + hours * 3600)
    raw = body.encode() if isinstance(body, str) else body
    return f"{expires}.{_sign(api_key, method, path, raw, expires)}"


def check(api_key: str, token: str, method: str, path: str, body: bytes, now: float | None = None) -> bool:
    try:
        expires_text, signature = token.split(".", 1)
        expires = int(expires_text)
    except (ValueError, AttributeError):
        return False
    if expires < (now or time.time()):
        return False
    return hmac.compare_digest(signature, _sign(api_key, method, path, body, expires))
