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


def people(settings: "Settings") -> dict[str, dict[str, list[str]]]:
    """SIRISAI_PEOPLE_ACCOUNTS (7.27): everyone else's names, per person, e.g.
    "sam: plex=Sam,samantha; calendar=Family | jo: plex=Jo". The person is the
    user id voice recognition gives (SIRISAI_VOICE_ROLES names them too)."""
    out: dict[str, dict[str, list[str]]] = {}
    for block in (getattr(settings, "people_accounts", "") or "").split("|"):
        person, sep, rest = block.partition(":")
        if not sep or not person.strip():
            continue
        mapping: dict[str, list[str]] = {}
        for part in rest.split(";"):
            service, eq, found = part.partition("=")
            if eq and service.strip():
                mapping.setdefault(service.strip().lower(), []).extend(n.strip() for n in found.split(",") if n.strip())
        out[person.strip().lower()] = mapping
    return out


def names(settings: "Settings", service: str, user_id: str | None = None) -> list[str]:
    """Your names on `service`, or (7.27) whoever is speaking: voice recognition sets
    the turn's user, so "my history" is theirs when they ask. Someone with no
    accounts listed is looked up by their user id on Plex."""
    from app.core.user_context import DEFAULT_USER_ID, get_current_user_id

    who = (user_id or get_current_user_id() or DEFAULT_USER_ID).strip().lower()
    everyone = people(settings)
    # Until anyone else is listed, every turn is yours (as before 7.27).
    if everyone and who and who != DEFAULT_USER_ID.lower() and who not in {o.lower() for o in owners(settings)}:
        mine = everyone.get(who, {})
        if service.lower() in mine:
            return mine[service.lower()]
        return [who] if service.lower() == "plex" else []
    return accounts(settings).get(service.lower(), [])


def owners(settings: "Settings") -> list[str]:
    """User ids that are the owner (whose accounts are SIRISAI_MY_ACCOUNTS): SIRISAI_OWNER_USER_IDS."""
    return [o.strip() for o in (getattr(settings, "owner_user_ids", "") or "").split(",") if o.strip()]


def is_me(who: str | None) -> bool:
    return (who or "").strip().lower() in ME_WORDS


def matches(row_user: str | None, wanted: list[str]) -> bool:
    """Whether a row's user (a display name or username) is one of `wanted`, ignoring case."""
    have = (row_user or "").strip().lower()
    return bool(have) and have in {w.lower() for w in wanted}
