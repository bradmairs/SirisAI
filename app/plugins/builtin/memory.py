"""Memory tools -- how the LLM reads and writes SirisAI's long-term memory
during a conversation (see app/memory/service.py for the storage itself).
"""

from typing import Any

from app.memory.service import MemoryStore
from app.plugins.base import Tool


class RememberTool(Tool):
    name = "remember"
    description = (
        "Save a piece of information to long-term memory for later recall "
        "-- facts about the user, their hobbies, homelab setup, preferences, etc."
    )
    parameters = {
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "The information to remember."},
        },
        "required": ["content"],
    }

    def __init__(self, memory: MemoryStore) -> None:
        self._memory = memory

    def run(self, content: str = "", **kwargs: Any) -> Any:
        return self._memory.remember(content)


class RecallTool(Tool):
    name = "recall"
    description = (
        "Search long-term memory for previously saved information about the "
        "user. Omit the query to get the most recently saved memories."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Optional search text."},
        },
        "required": [],
    }

    def __init__(self, memory: MemoryStore) -> None:
        self._memory = memory

    def run(self, query: str | None = None, **kwargs: Any) -> Any:
        return self._memory.recall(query=query)
