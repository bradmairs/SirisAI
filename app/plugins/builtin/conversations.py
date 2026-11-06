"""Search and browse past conversations."""

from typing import Any

from app.conversation.service import ConversationStore
from datetime import datetime, timedelta

from app.core.clock import get_zone, to_local
from app.plugins.base import Tool, ToolPermission


def _end_of(day: str | None, zone: Any) -> str | None:
    """The end of a local date, as an instant (the search's `until` is exclusive)."""
    if not day:
        return None
    if len(day) > 10:
        return day
    return (datetime.fromisoformat(day).replace(tzinfo=zone) + timedelta(days=1)).isoformat()


class SearchConversationsTool(Tool):
    name = "search_conversations"
    description = (
        "Search what the user and SirisAI said in past conversations, newest first. Use for 'what did we talk "
        "about', 'what did I ask you about the NAS', 'when did I mention X'. For a time ('yesterday', 'last "
        "week') give since/until as dates: you then get one result per conversation with its title and a "
        "short summary of what came of it."
    )
    permission = ToolPermission.READ
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "A word or phrase to look for."},
            "limit": {"type": "integer", "description": "How many matches to return. Defaults to 8."},
            "since": {"type": "string", "description": "Optional: from this date (YYYY-MM-DD), local."},
            "until": {"type": "string", "description": "Optional: up to and including this date (YYYY-MM-DD), local."},
        },
        "required": [],
    }

    def __init__(self, conversations: ConversationStore, timezone: str, siris: Any = None) -> None:
        self._conversations = conversations
        self._timezone = timezone
        self._siris = siris

    def run(self, query: str = "", limit: int = 8, since: str | None = None, until: str | None = None, **kwargs: Any) -> Any:
        limit = max(1, min(int(limit), 30))
        if since or until or not query.strip():
            from app.conversation import summaries

            zone = get_zone(self._timezone)
            local = lambda d: datetime.fromisoformat(d).replace(tzinfo=zone).isoformat() if d and len(d) <= 10 else d  # noqa: E731
            found = summaries.search(self._siris, query, local(since), _end_of(until, zone), limit=limit)
            return [{**c, "when": to_local(c["last_at"], self._timezone)} for c in found]
        return [
            {**m, "when": to_local(m["when"], self._timezone)}
            for m in self._conversations.search_messages(query, limit=limit)
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
