"""Keep secrets out of everything SirisAI writes down (4.28.0).

4.27.1 found Tautulli's API key in error messages: httpx puts the whole
request URL in its errors, and Tautulli takes its key in the URL. That
message reached the model, the audit log and the inbox. Tautulli was fixed
on its own; this is the net under every integration:

- every configured setting whose name says it's a secret (token, key,
  password, secret...) is registered when SirisAI starts, and its value is
  replaced with `<secret>` wherever it shows up;
- so is anything that looks like a credential in a URL (`?apikey=...`,
  `&token=...`, `user:pass@host`) or a header (`Bearer ...`).

`scrub()` runs on tool errors (before the model or the audit log sees
them), on what's stored in the audit log, on feed items and on GitHub
issues SirisAI files.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.core.config import Settings

MASK = "<secret>"
_SECRET_FIELD = re.compile(r"(token|api_?key|password|passwd|secret|credential|private_key|_pin$)", re.I)
_URL_PARAM = re.compile(r"([?&](?:api_?key|apikey|token|access_token|key|password|secret|sig|signature|auth)=)[^&\s'\"<>]+", re.I)
_URL_USERINFO = re.compile(r"(https?://)[^/\s:@]+:[^/\s@]+@")
_BEARER = re.compile(r"(Bearer\s+)[A-Za-z0-9._~+/=-]{8,}")
_values: set[str] = set()


def register(settings: "Settings") -> int:
    """Remember every secret-looking setting's value (at least 6 characters, so "t" in tests isn't masked)."""
    found = 0
    for name, value in vars(settings).items():
        if isinstance(value, str) and len(value) >= 6 and _SECRET_FIELD.search(name):
            _values.add(value)
            found += 1
    return found


def add(value: str | None) -> None:
    if value and len(value) >= 6:
        _values.add(value)


def scrub(value: Any) -> Any:
    """`value` with secrets masked; strings, and strings inside lists and dicts."""
    if isinstance(value, str):
        text = value
        for secret in sorted(_values, key=len, reverse=True):
            if secret in text:
                text = text.replace(secret, MASK)
        text = _URL_PARAM.sub(lambda m: m.group(1) + MASK, text)
        text = _URL_USERINFO.sub(lambda m: m.group(1) + MASK + "@", text)
        return _BEARER.sub(lambda m: m.group(1) + MASK, text)
    if isinstance(value, dict):
        return {k: scrub(v) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub(v) for v in value]
    return value


class ScrubbedError(RuntimeError):
    """A tool's error with its secrets taken out (the original is never shown)."""
