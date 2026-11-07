"""Request-scoped "who is talking" (milestone 46) -- a contextvar rather
than threading `user_id` through every Tool.run/Skill.run signature,
since SirisAI and its tools are process-wide singletons (app/core/siris.py)
built once at startup, not per request; a real parameter would touch
every tool and skill in app/plugins/builtin/ and app/skills/builtin/ for
one column.

`resolve_user_id` is wired into every authenticated router alongside
`require_api_key` and sets this from the caller-supplied `X-Siris-User`
header. `MemoryStore`/`GoalStore` methods (app/memory/service.py,
app/goals/service.py) default their own `user_id` keyword argument to
`get_current_user_id()` when a caller doesn't pass one explicitly, so
every existing route, tool, and skill that already just calls e.g.
`siris.memory.remember(...)` or `siris.goals.list_goals(...)` keeps
working completely unchanged and automatically scopes to the right user.

No identity beyond the header's raw string is verified here -- same trust
model as `SIRISAI_API_KEY` (app/core/auth.py): one shared secret gates the
whole API, and whoever holds it is trusted to say honestly which user
they're acting as. Absent the header (or in-process code with no request
in scope, e.g. a test constructing SirisAI() directly), everything falls
back to DEFAULT_USER_ID, so a single-user deployment -- the default,
unconfigured case -- behaves exactly as it did before this milestone.
"""

from contextvars import ContextVar

from fastapi import Header

DEFAULT_USER_ID = "default"

_current_user_id: ContextVar[str] = ContextVar("current_user_id", default=DEFAULT_USER_ID)


def get_current_user_id() -> str:
    return _current_user_id.get()


def set_current_user_id(user_id: str) -> None:
    """Switch who this request is acting as -- used when a voice turn
    recognises the speaker (app/voice/voiceprints.py) and no explicit
    X-Siris-User header was sent."""
    _current_user_id.set(user_id)


async def resolve_user_id(x_siris_user: str | None = Header(default=None)) -> str:
    """FastAPI dependency: reads X-Siris-User (blank/absent -> the default
    user) and sets it as the current request's user for the rest of this
    call -- store methods pick it up via get_current_user_id() without
    needing it passed through every layer in between."""
    user_id = x_siris_user.strip() if x_siris_user and x_siris_user.strip() else DEFAULT_USER_ID
    _current_user_id.set(user_id)
    return user_id


# What the person speaking may do this turn (4.9): "adult" (everything, the
# default), "child" or "guest" (no personal information and nothing that
# needs confirming). Set by a voice turn from who it recognised.
_turn_role: ContextVar[str] = ContextVar("siris_turn_role", default="adult")


def get_turn_role() -> str:
    return _turn_role.get()


def set_turn_role(role: str) -> None:
    _turn_role.set(role if role in ("adult", "child", "guest") else "adult")


def role_for(user_id: str | None, roles: str) -> str:
    """The role SIRISAI_VOICE_ROLES gives `user_id` ("dom=adult,sam=child")."""
    for pair in (roles or "").split(","):
        name, _, role = pair.partition("=")
        if user_id and name.strip().lower() == user_id.lower() and role.strip() in ("adult", "child", "guest"):
            return role.strip()
    return "adult"

