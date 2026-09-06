"""Rollback (trustworthiness infrastructure, milestone 70): reverses the
most recent call to a specific tool, for the tools this codebase's
verify() sweep (milestones 38/65/68/69) already knows have a real, safe
inverse -- adding a movie/series has a delete tool right next to it, and
a Home Assistant device-control call has an obvious opposite service.

Deliberately narrow: `_INVERSE_HANDLERS` is a fixed, explicit allowlist,
not a generic "figure out how to undo any tool" mechanism -- guessing an
inverse for an arbitrary tool (what's the opposite of a Docker restart? a
Hermes-delegated shell command?) would be worse than admitting it isn't
supported. A tool not in the list gets exactly that answer, not a guess.

CONFIRM_REQUIRED, same tier as the underlying delete/control calls this
reverses -- but the single confirmation of *this* call covers the one,
already-identified inner action it performs (the audit log entry looked
up is named in the trigger), the same reasoning PresenceSkill's scene
activation already uses for its own auto-confirmed inner call: the
narrow, specific thing being approved *is* the approval, not a blank
check to do anything.
"""

from typing import TYPE_CHECKING, Any

from app.skills.base import Skill

if TYPE_CHECKING:
    from app.core.siris import SirisAI

# service -> its opposite. Only for domain-independent boolean-ish
# services with one unambiguous inverse -- the same set
# HomeAssistantCallServiceTool.verify()'s _EXPECTED_STATE_BY_SERVICE
# already trusts to have a single correct state. "disarm" has no safe
# single inverse (which arm mode was it in before?) so it's absent
# rather than guessed at.
_HOME_ASSISTANT_INVERSE_SERVICE: dict[str, str] = {
    "turn_on": "turn_off",
    "turn_off": "turn_on",
    "lock": "unlock",
    "unlock": "lock",
    "open_cover": "close_cover",
    "close_cover": "open_cover",
}


def _inverse_radarr_add_movie(
    arguments: dict[str, Any], result: Any
) -> tuple[str, dict[str, Any]] | None:
    movie_id = result.get("id") if isinstance(result, dict) else None
    if not movie_id:
        return None
    return "radarr_delete_movie", {"movie_id": movie_id}


def _inverse_sonarr_add_series(
    arguments: dict[str, Any], result: Any
) -> tuple[str, dict[str, Any]] | None:
    series_id = result.get("id") if isinstance(result, dict) else None
    if not series_id:
        return None
    return "sonarr_delete_series", {"series_id": series_id}


def _inverse_home_assistant_call_service(
    arguments: dict[str, Any], result: Any
) -> tuple[str, dict[str, Any]] | None:
    domain = arguments.get("domain")
    entity_id = arguments.get("entity_id")
    inverse_service = _HOME_ASSISTANT_INVERSE_SERVICE.get(arguments.get("service", ""))
    if not domain or not entity_id or not inverse_service:
        return None
    return "home_assistant_call_service", {
        "domain": domain,
        "service": inverse_service,
        "entity_id": entity_id,
    }


def _inverse_unifi_block_client(
    arguments: dict[str, Any], result: Any
) -> tuple[str, dict[str, Any]] | None:
    client_id = arguments.get("client_id")
    if not client_id:
        return None
    return "unifi_unblock_client", {"client_id": client_id}


def _inverse_unifi_unblock_client(
    arguments: dict[str, Any], result: Any
) -> tuple[str, dict[str, Any]] | None:
    client_id = arguments.get("client_id")
    if not client_id:
        return None
    return "unifi_block_client", {"client_id": client_id}


_INVERSE_HANDLERS = {
    "radarr_add_movie": _inverse_radarr_add_movie,
    "sonarr_add_series": _inverse_sonarr_add_series,
    "home_assistant_call_service": _inverse_home_assistant_call_service,
    "unifi_block_client": _inverse_unifi_block_client,
    "unifi_unblock_client": _inverse_unifi_unblock_client,
}


class RollbackLastActionSkill(Skill):
    name = "rollback_last_action"
    description = (
        "Reverse the most recent call to a specific tool, if that tool has a known, safe "
        "inverse -- e.g. deleting a movie radarr_add_movie just added, or calling the opposite "
        "Home Assistant service (turn_on -> turn_off, lock -> unlock). Only tools with a "
        "registered inverse are supported; anything else is reported as unsupported rather "
        "than guessed at."
    )
    parameters = {
        "type": "object",
        "properties": {
            "tool_name": {
                "type": "string",
                "description": "The tool whose most recent call to reverse, e.g. 'radarr_add_movie'.",
            },
        },
        "required": ["tool_name"],
    }

    def run(self, siris: "SirisAI", tool_name: str = "", **kwargs: Any) -> Any:
        handler = _INVERSE_HANDLERS.get(tool_name)
        if handler is None:
            return {
                "rolled_back": False,
                "reason": f"'{tool_name}' has no registered rollback. Supported: {sorted(_INVERSE_HANDLERS)}.",
            }

        recent = siris.audit.find_recent(tool_name, limit=1)
        if not recent:
            return {
                "rolled_back": False,
                "reason": f"No recorded call to '{tool_name}' found.",
            }

        entry = recent[0]
        if entry["error"] is not None:
            return {
                "rolled_back": False,
                "reason": f"That call itself failed ({entry['error']}) -- nothing to undo.",
            }

        inverse = handler(entry["arguments"], entry["result"])
        if inverse is None:
            return {
                "rolled_back": False,
                "reason": (
                    f"The recorded '{tool_name}' call (audit id {entry['id']}) doesn't have "
                    "enough information to reverse."
                ),
            }

        inverse_name, inverse_arguments = inverse
        result = siris.run_tool(
            inverse_name,
            inverse_arguments,
            confirmed=True,
            trigger=f"rollback_last_action: undoing {tool_name} (audit id {entry['id']}), itself confirmed",
            evidence=entry,
        )
        return {
            "rolled_back": True,
            "undid": tool_name,
            "undo_tool": inverse_name,
            "undo_arguments": inverse_arguments,
            "result": result,
        }
