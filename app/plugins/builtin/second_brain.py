"""Second Brain tools, via app/second_brain/client.py. Registered only when
SIRISAI_BRAIN_PATH points at a checkout of Siris-Second-Brain.

brain_search/brain_get_note/brain_insights are READ. brain_learn is
SAFE_ACTION, not CONFIRM_REQUIRED like obsidian_create_or_update_note: the
vault's engine
only ever adds (a new note, or a dated update appended to an existing
one) and never replaces content, and with git sync every write is a
commit -- routine and reversible, the same tier as `remember`.

`mirror_to_brain()` is what makes "every time Siris learns something, it
goes in the brain" automatic rather than left to the model: `remember`
and `remember_entity_fact` call it after their own store succeeds (see
app/plugins/builtin/memory.py), so facts reach the vault whichever tool
the model picks.
"""

import logging
import re
from typing import Any

from app.plugins.base import Tool, ToolPermission, ToolVerification
from app.second_brain.client import SecondBrainClient

logger = logging.getLogger(__name__)

# Where plain `remember` facts land: one rolling inbox note, filed into
# proper notes later by the vault's /process-inbox workflow.
MEMORY_INBOX_TITLE = "Siris Memory Inbox"
MEMORY_TAG = "siris-memory"

_NOTE_TYPES = ["note", "person", "resource", "project", "area", "capture"]
# "auto" (the default) lets the vault's router decide the type, folder and life areas.
_TYPE_CHOICES = ["auto", *_NOTE_TYPES]
_WIKILINK = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|([^\]]*))?\]\]")


def _plain(text: str) -> str:
    """Note text without link markup and with whitespace collapsed --
    the engine turns mentions into [[links]] as it writes, so that's the
    form written content can be compared in."""
    text = _WIKILINK.sub(lambda m: m.group(2) or m.group(1), text)
    return re.sub(r"\s+", " ", text).strip().lower()


def mirror_to_brain(brain: SecondBrainClient | None, title: str, content: str, **fields: Any) -> dict[str, Any] | None:
    """Copy a memory into the brain. Never raises: the memory store already
    has the fact, so a vault problem is reported, not turned into a failed
    `remember`."""
    if brain is None or not content.strip():
        return None
    try:
        tags = sorted({MEMORY_TAG, *fields.pop("tags", [])})
        result = brain.learn([{"title": title, "content": content, "tags": tags, **fields}])[0]
        return {"saved": True, "action": result["action"], "title": result["title"], "path": result["path"],
                "areas": result.get("areas", [])}
    except Exception as exc:  # noqa: BLE001 - any vault failure is reported, not raised
        logger.warning("Could not mirror memory to the second brain: %s", exc)
        return {"saved": False, "error": f"{type(exc).__name__}: {exc}"[:200]}


class BrainSearchTool(Tool):
    name = "brain_search"
    description = (
        "Search Brad's second brain (Siris): his long-term vault of linked notes about his life, family, "
        "work, projects, home lab, fitness and interests. Use it before answering anything about Brad's own "
        "world. Each result gives a `title` and `path` (cite the title as the source, e.g. \"According to "
        "your note 'Home Server': ...\") and `excerpts`."
    )
    permission = ToolPermission.READ
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Words or a name to look for, e.g. 'marathon' or 'Dom'."},
            "limit": {"type": "integer", "description": "Max results. Defaults to 8."},
        },
        "required": ["query"],
    }

    def __init__(self, client: SecondBrainClient) -> None:
        self._client = client

    def run(self, query: str = "", limit: int = 8, **kwargs: Any) -> Any:
        return self._client.search(query, limit=limit)


class BrainGetNoteTool(Tool):
    name = "brain_get_note"
    description = (
        "Read one note from Brad's second brain in full, by title or alias (e.g. 'Dom Mairs', 'Dom', "
        "'Home Server'), with the notes it links to and the notes that link back to it."
    )
    permission = ToolPermission.READ
    parameters = {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "The note's title or one of its aliases."},
        },
        "required": ["title"],
    }

    def __init__(self, client: SecondBrainClient) -> None:
        self._client = client

    def run(self, title: str = "", **kwargs: Any) -> Any:
        note = self._client.get_note(title)
        if note is None:
            return {"found": False, "title": title, "hint": "Try brain_search to find the right title."}
        return {"found": True, **note}


class BrainInsightsTool(Tool):
    name = "brain_insights"
    description = (
        "What Brad's second brain can tell about itself right now: how much it grew this week against "
        "last, growing topics, upcoming project deadlines, overdue tasks, projects nobody has touched in "
        "weeks, how old the inbox is, and notes that look related but aren't linked yet. Use it for "
        "\"what's going on in my brain?\", \"what am I neglecting?\", or to plan a weekly review. "
        "`highlights` are ready-made sentences; the other fields hold the detail."
    )
    permission = ToolPermission.READ
    parameters = {
        "type": "object",
        "properties": {
            "days": {"type": "integer", "description": "How many days of activity to look at. Defaults to 30."},
        },
    }

    def __init__(self, client: SecondBrainClient) -> None:
        self._client = client

    def run(self, days: int = 30, **kwargs: Any) -> Any:
        return self._client.insights(days=max(7, min(int(days or 30), 365)))


class BrainLearnTool(Tool):
    name = "brain_learn"
    description = (
        "Add knowledge to Brad's second brain as a linked note: something durable and worth keeping that "
        "you learned, e.g. a fact, preference, decision, plan, how something works, or a person. Use one "
        "call per idea and title it as the idea or the thing it's about ('Melbourne Marathon 2026 10 km', "
        "'Home Server'). If a note with that title or alias exists, this appends a dated update instead of "
        "duplicating it; it never overwrites. Name related things in the content: existing notes get "
        "linked automatically."
    )
    permission = ToolPermission.SAFE_ACTION
    parameters = {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Note title: the idea, person, project or thing."},
            "content": {"type": "string", "description": "What was learned, in plain words (markdown)."},
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional lowercase tags, e.g. ['fitness', 'running'].",
            },
            "source": {"type": "string", "description": "Optional: where this came from (a URL, 'Brad said', ...)."},
            "type": {
                "type": "string",
                "enum": _TYPE_CHOICES,
                "description": "Optional. Leave as auto and the brain decides where it belongs (person, project, "
                "resource, note or inbox) and which life areas it's part of; set it only to override.",
            },
            "links": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional titles of notes to link explicitly.",
            },
        },
        "required": ["title", "content"],
    }

    def __init__(self, client: SecondBrainClient) -> None:
        self._client = client

    def _item(self, title: str, content: str, tags: list[str] | None, source: str, type: str, links: list[str] | None) -> dict[str, Any]:
        item: dict[str, Any] = {"title": title, "content": content, "tags": list(tags or [])}
        if source:
            item["source"] = source
        if type in _NOTE_TYPES:
            item["type"] = type
        if links:
            item["links"] = list(links)
        return item

    def run(
        self,
        title: str = "",
        content: str = "",
        tags: list[str] | None = None,
        source: str = "",
        type: str = "auto",
        links: list[str] | None = None,
        **kwargs: Any,
    ) -> Any:
        if not title.strip() or not content.strip():
            return {"saved": False, "error": "Both a title and some content are needed."}
        result = self._client.learn([self._item(title, content, tags, source, type, links)])[0]
        return {"saved": True, **result}

    def dry_run(self, title: str = "", content: str = "", type: str = "auto", **kwargs: Any) -> Any:
        """The router's decision, without writing: whether it creates or
        updates (an alias can land on a differently-titled note), where,
        as what type, in which life areas, and why."""
        decision = self._client.route(title, content, type=type if type in _TYPE_CHOICES else "auto")
        return {
            "title": title,
            "would_create": decision["action"] == "create",
            "would_update": decision["action"] == "update",
            "existing_title": decision["title"] if decision["action"] == "update" else None,
            "path": f"{decision['folder']}/{decision['title']}.md",
            "type": decision["type"],
            "areas": decision["areas"],
            "reasons": decision["reasons"],
        }

    def verify(self, arguments: dict[str, Any], result: Any) -> ToolVerification:
        """Re-reads the note and checks the written content is in it,
        compared without link markup: the engine links mentions as it
        writes, so the stored text legitimately differs from what was sent
        by its [[brackets]]."""
        if not isinstance(result, dict) or not result.get("saved"):
            return ToolVerification(verified=False, detail="Nothing was saved.")
        note = self._client.get_note(result["title"])
        if note is None:
            return ToolVerification(verified=False, detail=f"'{result['title']}' isn't in the vault after writing.")
        if _plain(arguments.get("content", "")) in _plain(note["body"]):
            return ToolVerification(verified=True, detail=f"'{note['title']}' contains the new content.")
        return ToolVerification(verified=False, detail=f"'{note['title']}' doesn't contain the new content.")
