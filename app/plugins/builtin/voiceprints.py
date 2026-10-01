"""Voiceprint tools (app/voice/voiceprints.py): who SirisAI can recognise by
voice. Enrolling needs a recording, so it happens through
POST /siris/voice/voiceprints/enroll (the web UI's Status tab), not chat."""

from typing import Any

from app.plugins.base import Tool, ToolPermission
from app.voice.voiceprints import VoiceprintStore


class ListVoiceprintsTool(Tool):
    name = "list_voiceprints"
    description = "List the household members SirisAI can recognise by voice, and how many samples each has enrolled."
    permission = ToolPermission.READ
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, store: VoiceprintStore) -> None:
        self._store = store

    def run(self, **kwargs: Any) -> Any:
        return self._store.list_voiceprints()


class DeleteVoiceprintTool(Tool):
    name = "delete_voiceprint"
    description = "Forget a person's enrolled voice, so SirisAI no longer recognises them by voice."
    permission = ToolPermission.SAFE_ACTION
    parameters = {
        "type": "object",
        "properties": {"user_id": {"type": "string", "description": "The person's user id, from list_voiceprints."}},
        "required": ["user_id"],
    }

    def __init__(self, store: VoiceprintStore) -> None:
        self._store = store

    def run(self, user_id: str = "", **kwargs: Any) -> Any:
        return {"user_id": user_id, "deleted": self._store.delete(user_id)}
