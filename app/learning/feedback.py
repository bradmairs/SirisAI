"""Reading outcomes back: what the learning loop changes in SirisAI's
behaviour.

- **Autonomy demotion.** An action SirisAI takes on its own that the user has
  undone or declined most of the times it ran (3+ times, more often than it
  was kept or approved, over the last 60 days) is held for confirmation next
  time, instead of just happening. Asking once in the attention feed beats
  doing the wrong thing again. It earns its way back: each approval in the
  feed counts, and once it's kept more than undone it runs unasked again.
- **Quieter suggestions.** A kind of suggestion the user keeps turning down
  (5+ dismissals, under 20% accepted) stops being pushed to the phone. It
  still sits in the attention feed, so nothing is lost.
"""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.core.siris import SirisAI

DEMOTE_MIN_UNDOS = 3
MUTE_MIN_DISMISSALS = 5
MUTE_MAX_APPROVAL = 0.2


def action_key(tool_name: str, arguments: dict[str, Any]) -> str:
    """The specific action, so undoing "turn on the office light" doesn't
    demote every Home Assistant call."""
    if tool_name == "home_assistant_call_service":
        entity = arguments.get("entity_id")
        if isinstance(entity, list):
            entity = ",".join(sorted(map(str, entity)))
        return f"{arguments.get('domain')}.{arguments.get('service')}:{entity or '-'}"
    if tool_name == "docker_restart_container":
        return f"docker_restart_container:{arguments.get('container')}"
    if tool_name in ("run_protocol", "run_scene"):
        return f"{tool_name}:{str(arguments.get('name', '')).lower()}"
    return tool_name


def demotion(siris: "SirisAI", tool_name: str, arguments: dict[str, Any]) -> str | None:
    """Why this autonomous action should wait for the user's OK, or None."""
    store = getattr(siris, "outcomes", None)
    if store is None:
        return None
    key = action_key(tool_name, arguments)
    try:
        c = store.counts(key=key)
    except Exception:  # never let the learning loop block a real action
        return None
    bad, good = c["undone"] + c["dismissed"], c["kept"] + c["accepted"]
    if bad >= DEMOTE_MIN_UNDOS and bad > good:
        return f"you undid or turned down this {bad} of the last {bad + good} times I did it on my own"
    return None


def demoted_keys(siris: "SirisAI") -> list[str]:
    """Every action currently held for confirmation by demotion()."""
    store = getattr(siris, "outcomes", None)
    if store is None:
        return []
    out = []
    for key in store.keys_with(("undone", "dismissed")):
        c = store.counts(key=key)
        bad, good = c["undone"] + c["dismissed"], c["kept"] + c["accepted"]
        if bad >= DEMOTE_MIN_UNDOS and bad > good:
            out.append(key)
    return out


def muted(siris: "SirisAI", subject: str) -> bool:
    """Whether to stop pushing this kind of suggestion to the phone."""
    store = getattr(siris, "outcomes", None)
    if store is None:
        return False
    try:
        c = store.counts(subject=subject)
    except Exception:
        return False
    total = c["accepted"] + c["dismissed"]
    return c["dismissed"] >= MUTE_MIN_DISMISSALS and c["accepted"] / total < MUTE_MAX_APPROVAL
