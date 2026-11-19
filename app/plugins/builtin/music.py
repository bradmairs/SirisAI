"""Music by voice (7.27), through Home Assistant.

Playing, pausing, skipping and volume on one player were already here
(home_assistant_media_control). This adds the rest:

- **music_play**: "play some jazz in the lounge", "play Fleetwood Mac". Uses
  Music Assistant's `music_assistant.play_media` (search by name across your
  music providers). Without Music Assistant, Home Assistant can't search
  music by name, so it says so rather than guessing.
- **music_now_playing**: "what's this song?" -- every player that's playing,
  with the title, artist and album.
- **music_pause_all**: "pause everything".
- **music_move**: "move the music to the kitchen" (Music Assistant's
  transfer_queue). With SIRISAI_MUSIC_FOLLOW_ME on, music moves by itself
  when you start talking to a satellite in another room.

The room comes from what you said, else the satellite you're talking to,
else the only player that's on.
"""

from __future__ import annotations

from typing import Any

from app.plugins.base import Tool, ToolPermission

_FILLER = {"the", "in", "on", "speaker", "speakers", "room", "my"}


def players(client: Any) -> list[dict[str, Any]]:
    return client.list_states(domain="media_player")


def _name(state: dict[str, Any]) -> str:
    return (state.get("attributes") or {}).get("friendly_name") or state["entity_id"].split(".", 1)[1].replace("_", " ")


def find_player(client: Any, room: str | None, current_room: str | None = None) -> dict[str, Any] | None:
    """The player for `room` (or the satellite's room), else the only one that's on."""
    from app.plugins.builtin.home_assistant import _entity_name_matches

    found = players(client)
    for hint in (room, current_room):
        words = [w for w in (hint or "").lower().split() if w not in _FILLER]
        if not words:
            continue
        matches = [p for p in found if _entity_name_matches(words, f"{p['entity_id']} {_name(p)}".lower())]
        if len(matches) >= 1:
            return sorted(matches, key=lambda p: p["state"] in ("playing", "paused"), reverse=True)[0]
    on = [p for p in found if p.get("state") not in ("off", "unavailable", "unknown")]
    return on[0] if len(on) == 1 else (found[0] if len(found) == 1 else None)


class _Music(Tool):
    def __init__(self, siris: Any) -> None:
        self._siris = siris

    @property
    def _ha(self) -> Any:
        return getattr(self._siris, "_home_assistant_client", None)

    def _room(self) -> str | None:
        try:
            return self._siris.satellites.current_room()
        except Exception:
            return None


class MusicPlayTool(_Music):
    name = "music_play"
    description = (
        "Play music by name on a speaker: an artist, album, song, playlist, radio station or a genre/mood "
        "('play some jazz in the lounge', 'play Fleetwood Mac', 'put on something relaxing'). Needs Music Assistant."
    )
    permission = ToolPermission.SAFE_ACTION
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "What to play, e.g. 'Fleetwood Mac', 'relaxing jazz', 'BBC Radio 6'."},
            "room": {"type": "string", "description": "Where, e.g. 'lounge'. Defaults to the room you're in."},
            "media_type": {"type": "string", "enum": ["artist", "album", "track", "playlist", "radio"], "description": "If known."},
        },
        "required": ["query"],
    }

    def run(self, query: str = "", room: str | None = None, media_type: str | None = None, **kwargs: Any) -> Any:
        client = self._ha
        if client is None:
            raise RuntimeError("Home Assistant isn't set up.")
        player = find_player(client, room, self._room())
        if player is None:
            raise ValueError("Which speaker? I couldn't tell which room you meant.")
        data: dict[str, Any] = {"media_id": query.strip()}
        if media_type:
            data["media_type"] = media_type
        try:
            client.call_service("music_assistant", "play_media", player["entity_id"], **data)
        except Exception as exc:
            if "400" in str(exc) or "404" in str(exc):
                raise RuntimeError(
                    "Playing music by name needs Music Assistant in Home Assistant (music_assistant.play_media wasn't there)."
                ) from None
            raise
        return {"playing": query, "on": _name(player), "entity_id": player["entity_id"]}


class MusicNowPlayingTool(_Music):
    name = "music_now_playing"
    description = "What's playing on the speakers and TVs right now ('what's this song?', 'who sings this?'): title, artist, album, where."
    permission = ToolPermission.READ
    parameters = {"type": "object", "properties": {}, "required": []}

    def run(self, **kwargs: Any) -> Any:
        client = self._ha
        if client is None:
            raise RuntimeError("Home Assistant isn't set up.")
        out = []
        for p in players(client):
            if p.get("state") != "playing":
                continue
            a = p.get("attributes") or {}
            out.append({"where": _name(p), "title": a.get("media_title"), "artist": a.get("media_artist"), "album": a.get("media_album_name")})
        here = (self._room() or "").lower()
        out.sort(key=lambda x: here and here in x["where"].lower(), reverse=True)
        return out or {"playing": [], "note": "Nothing is playing."}


class MusicPauseAllTool(_Music):
    name = "music_pause_all"
    description = "Pause everything that's playing in the house ('pause everything', 'quiet the whole house')."
    permission = ToolPermission.SAFE_ACTION
    parameters = {"type": "object", "properties": {}, "required": []}

    def run(self, **kwargs: Any) -> Any:
        client = self._ha
        if client is None:
            raise RuntimeError("Home Assistant isn't set up.")
        paused = []
        for p in players(client):
            if p.get("state") == "playing":
                client.call_service("media_player", "media_pause", p["entity_id"])
                paused.append(_name(p))
        return {"paused": paused}


class MusicMoveTool(_Music):
    name = "music_move"
    description = "Move what's playing to another room's speaker ('move the music to the kitchen'). Needs Music Assistant."
    permission = ToolPermission.SAFE_ACTION
    parameters = {
        "type": "object",
        "properties": {"to_room": {"type": "string", "description": "Where to move it, e.g. 'kitchen'."}},
        "required": ["to_room"],
    }

    def run(self, to_room: str = "", **kwargs: Any) -> Any:
        return move(self._siris, to_room)


def move(siris: Any, to_room: str, from_player: str | None = None) -> dict[str, Any]:
    client = getattr(siris, "_home_assistant_client", None)
    if client is None:
        raise RuntimeError("Home Assistant isn't set up.")
    target = find_player(client, to_room)
    playing = [p for p in players(client) if p.get("state") == "playing"]
    source = next((p for p in playing if p["entity_id"] == from_player), None) or (playing[0] if playing else None)
    if target is None:
        raise ValueError(f"I couldn't find a speaker in the {to_room}.")
    if source is None:
        return {"moved": False, "note": "Nothing is playing."}
    if source["entity_id"] == target["entity_id"]:
        return {"moved": False, "note": f"It's already playing on the {_name(target)}."}
    client.call_service("music_assistant", "transfer_queue", target["entity_id"], source_player=source["entity_id"])
    return {"moved": True, "from": _name(source), "to": _name(target)}


def follow(siris: Any) -> dict[str, Any] | None:
    """SIRISAI_MUSIC_FOLLOW_ME: move the music to the room you last spoke in, if it's elsewhere."""
    if not siris.settings.music_follow_me or getattr(siris, "_home_assistant_client", None) is None:
        return None
    room = siris.satellites.current_room()
    last = siris.state.get("music:follow_room")
    if not room or room == last:
        return None
    siris.state.set("music:follow_room", room)
    if last is None:
        return None
    client = siris._home_assistant_client
    source = find_player(client, last)
    if source is None or source.get("state") != "playing":
        return None
    try:
        return move(siris, room, source["entity_id"])
    except Exception:
        return None
