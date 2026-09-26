"""Search and browse past conversations."""

from typing import Any

from app.conversation.service import ConversationStore
from app.core.clock import to_local
from app.plugins.base import Tool, ToolPermission


class SearchConversationsTool(Tool):
    name = "search_conversations"
    description = (
        "Search what the user and SirisAI said in past conversations, newest first. Use for 'what did we talk "
        "about', 'what did I ask you about the NAS', 'when did I mention X'."
    )
    permission = ToolPermission.READ
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "A word or phrase to look for."},
            "limit": {"type": "integer", "description": "How many matches to return. Defaults to 8."},
        },
        "required": ["query"],
    }

    def __init__(self, conversations: ConversationStore, timezone: str) -> None:
        self._conversations = conversations
        self._timezone = timezone

    def run(self, query: str = "", limit: int = 8, **kwargs: Any) -> Any:
        return [
            {**m, "when": to_local(m["when"], self._timezone)}
            for m in self._conversations.search_messages(query, limit=max(1, min(int(limit), 30)))
        ]


class RecentConversationsTool(Tool):
    name = "recent_conversations"
    description = "List recent conversations (newest first) with what each started with. Use for 'what have we been talking about lately'."
    permission = ToolPermission.READ
    parameters = {
        "type": "object",
        "properties": {"limit": {"type": "integer", "description": "How many conversations. Defaults to 8."}},
        "required": [],
    }

    def __init__(self, conversations: ConversationStore, timezone: str) -> None:
        self._conversations = conversations
        self._timezone = timezone

    def run(self, limit: int = 8, **kwargs: Any) -> Any:
        return [
            {**c, "last_at": to_local(c["last_at"], self._timezone)}
            for c in self._conversations.recent_conversations(limit=max(1, min(int(limit), 30)))
        ]
