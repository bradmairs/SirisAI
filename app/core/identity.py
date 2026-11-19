"""Who "me" is on each service (7.17).

SIRISAI_MY_ACCOUNTS maps a service to your names on it, e.g.

    plex=Brad,brad6491; calendar=Personal

Each name can be a username, a display name or an email. Tools that can
narrow to one person (Plex history, stats and what's playing) take
user="me" and resolve it here, so "my" means the same account everywhere.
For Plex, SIRISAI_PLEX_USERNAME still works and is used when the map has no
plex entry; with neither, Plex falls back to the server's owner.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.core.config import Settings

ME_WORDS = frozenset({"me", "mine", "my", "myself", "i"})
_FALLBACKS = {"plex": "plex_username"}


def accounts(settings: "Settings") -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for part in (getattr(settings, "my_accounts", "") or "").replace("\n", ";").split(";"):
        service, sep, names = part.partition("=")
        if not sep or not service.strip():
            continue
        found = [n.strip() for n in names.split(",") if n.strip()]
        if found:
            out.setdefault(service.strip().lower(), []).extend(found)
    for service, field in _FALLBACKS.items():
        if service not in out:
            fallback = [n.strip() for n in (getattr(settings, field, "") or "").split(",") if n.strip()]
            if fallback:
                out[service] = fallback
    return out


def names(settings: "Settings", service: str) -> list[str]:
    return accounts(settings).get(service.lower(), [])


def is_me(who: str | None) -> bool:
    return (who or "").strip().lower() in ME_WORDS


def matches(row_user: str | None, wanted: list[str]) -> bool:
    """Whether a row's user (a display name or username) is one of `wanted`, ignoring case."""
    have = (row_user or "").strip().lower()
    return bool(have) and have in {w.lower() for w in wanted}
