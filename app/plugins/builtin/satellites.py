"""Push a reply into whichever room the user is currently in, rather
than always the device a conversation started on (milestone 48).
Registered whenever a TTS backend is configured -- same "empty = not
engaged" convention as everywhere else optional -- independent of
whether any satellite happens to be registered right now, since that's
runtime state, not configuration.
"""

from typing import Any

import httpx

from app.plugins.base import Tool, ToolPermission
from app.satellites.service import SatelliteStore
from app.voice import sync_tts


class SpeakInCurrentRoomTool(Tool):
    name = "speak_in_current_room"
    description = (
        "Speak a message aloud through whichever room's satellite the user is currently in -- "
        "the room they most recently spoke to a satellite from, not necessarily the device this "
        "conversation started on. Use for something worth interrupting them for (e.g. a proactive "
        "check found something), not routine confirmations. Does nothing (sent: false) if no "
        "satellite is currently known."
    )
    permission = ToolPermission.SAFE_ACTION
    parameters = {
        "type": "object",
        "properties": {"message": {"type": "string", "description": "What to say aloud."}},
        "required": ["message"],
    }

    def __init__(
        self,
        satellites: SatelliteStore,
        tts_base_url: str,
        tts_model: str,
        tts_voice: str,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._satellites = satellites
        self._tts_base_url = tts_base_url
        self._tts_model = tts_model
        self._tts_voice = tts_voice
        self._transport = transport  # test-only seam, real requests otherwise

    def run(self, message: str = "", **kwargs: Any) -> Any:
        room = self._satellites.current_room()
        if room is None:
            return {"sent": False, "reason": "no satellite currently known"}
        satellite = self._satellites.get_by_room(room)
        if satellite is None:  # a race between current_room() and get_by_room() -- vanishingly rare, still handled
            return {"sent": False, "reason": "no satellite currently known"}

        audio = sync_tts.synthesize(
            self._tts_base_url, self._tts_model, self._tts_voice, message, transport=self._transport
        )

        with httpx.Client(transport=self._transport) as client:
            response = client.post(
                f"{satellite['callback_url'].rstrip('/')}/play",
                content=audio,
                headers={"Content-Type": "audio/mpeg"},
                timeout=30.0,
            )
            response.raise_for_status()

        return {"sent": True, "room": room}
