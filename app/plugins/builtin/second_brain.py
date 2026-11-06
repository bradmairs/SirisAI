"""Second Brain tools, via app/second_brain/client.py. Registered only when
SIRISAI_BRAIN_PATH points at a checkout of Siris-Second-Brain.

brain_delete_note is CONFIRM_REQUIRED: deleting the wrong note on a
misheard "forget it" is worse than asking, even though brain_restore_note
(SAFE_ACTION) undoes it and permanent deletion isn't offered to the model.

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
from app.second_brain.client import SecondBrainClient, SecondBrainError

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
        "Search Siris, Brad's knowledge base: his long-term vault of linked notes about his life, family, "
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


class BrainPastProjectsTool(Tool):
    name = "brain_past_projects"
    description = (
        "Search Brad's past and current engineering work in Siris: project pages (GVW projects, and past WSP "
        "and Downer jobs from OneNote) and topic pages (Culverts, Stormwater and Hydrology, Contract "
        "Variations…), then the work notes behind them. Use it for questions like \"have I dealt with a "
        "culvert like this before?\", \"what did we do on Dellows Underslip?\" or \"what have I learned "
        "about variations?\". Answer with the projects and dates, citing the note titles."
    )
    permission = ToolPermission.READ
    parameters = {
        "type": "object",
        "properties": {"query": {"type": "string", "description": "What to look for, e.g. 'culvert outlet scour'."}},
        "required": ["query"],
    }

    def __init__(self, client: SecondBrainClient) -> None:
        self._client = client

    def run(self, query: str = "", **kwargs: Any) -> Any:
        try:
            found = self._client.search(query, limit=30)
        except SecondBrainError as exc:
            return {"error": str(exc)}
        is_page = lambda h: "/Projects/" in h.get("path", "") or h.get("path", "").startswith("Work/Topics/")  # noqa: E731
        work = [h for h in found if h.get("path", "").startswith("Work/")]
        return {"pages": [h for h in work if is_page(h)][:5], "notes": [h for h in work if not is_page(h)][:6]}


class BrainGetNoteTool(Tool):
    name = "brain_get_note"
    description = (
        "Read one note from Siris, Brad's knowledge base, in full, by title or alias (e.g. 'Dom Mairs', 'Dom', "
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
        "What Siris can tell about itself right now: how much it grew this week against "
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


class BrainLinkNotesTool(Tool):
    name = "brain_link_notes"
    description = (
        "Link two notes in Siris both ways, when he asks to connect them (\"link Pump "
        "Stations to Rising Mains\"). Linking by hand also overrides an earlier 'not related'."
    )
    permission = ToolPermission.SAFE_ACTION
    parameters = {
        "type": "object",
        "properties": {
            "a": {"type": "string", "description": "One note's title."},
            "b": {"type": "string", "description": "The other note's title."},
        },
        "required": ["a", "b"],
    }

    def __init__(self, client: SecondBrainClient) -> None:
        self._client = client

    def run(self, a: str = "", b: str = "", **kwargs: Any) -> Any:
        try:
            return self._client.link(a, b)
        except SecondBrainError as exc:
            return {"error": str(exc)}


class BrainMarkReviewedTool(Tool):
    name = "brain_mark_reviewed"
    description = (
        "Record that Brad checked a note SirisAI wrote in Siris and it's right (\"the Plex note "
        "looks right\", \"yes, the Home Server note is correct\"). It drops out of the weekly 'check "
        "these notes' list. For a correction, use brain_learn with what's right instead."
    )
    permission = ToolPermission.SAFE_ACTION
    parameters = {
        "type": "object",
        "properties": {"title": {"type": "string", "description": "The note's title."}},
        "required": ["title"],
    }

    def __init__(self, client: SecondBrainClient) -> None:
        self._client = client

    def run(self, title: str = "", **kwargs: Any) -> Any:
        try:
            return self._client.mark_reviewed(title)
        except SecondBrainError as exc:
            return {"error": str(exc)}


class BrainUnlinkNotesTool(Tool):
    name = "brain_unlink_notes"
    description = (
        "Disconnect two notes in Siris, when he says they shouldn't be linked (\"unlink X and "
        "Y\", \"those aren't related\"). Removes the link both ways (his words stay, just not as a link) and "
        "stops anything automatic from linking them again."
    )
    permission = ToolPermission.SAFE_ACTION
    parameters = BrainLinkNotesTool.parameters

    def __init__(self, client: SecondBrainClient) -> None:
        self._client = client

    def run(self, a: str = "", b: str = "", **kwargs: Any) -> Any:
        try:
            return self._client.unlink(a, b)
        except SecondBrainError as exc:
            return {"error": str(exc)}


class BrainMergeNotesTool(Tool):
    name = "brain_merge_notes"
    description = (
        "Merge one note in Siris into another on the same topic, when Brad agrees they're duplicates "
        "(\"merge GVW Project Closure into GVW Contract Closure\", or yes to insights' \"might be the same "
        "topic\"). Nothing is reworded: the first note's text goes onto the second as a dated update, links "
        "follow, and the first is archived. Use brain_notes_distinct when he says they're different."
    )
    permission = ToolPermission.SAFE_ACTION
    parameters = {
        "type": "object",
        "properties": {
            "source": {"type": "string", "description": "The note to fold in (it's archived)."},
            "into": {"type": "string", "description": "The note to keep."},
        },
        "required": ["source", "into"],
    }

    def __init__(self, client: SecondBrainClient) -> None:
        self._client = client

    def run(self, source: str = "", into: str = "", **kwargs: Any) -> Any:
        try:
            return self._client.merge(source, into)
        except SecondBrainError as exc:
            return {"error": str(exc)}


class BrainNotesDistinctTool(Tool):
    name = "brain_notes_distinct"
    description = (
        "Record that two notes in Siris that look alike are different things, when Brad says so (\"no, "
        "those are different\" to a 'might be the same topic' suggestion). They're never suggested for "
        "merging again; neither note changes."
    )
    permission = ToolPermission.SAFE_ACTION
    parameters = BrainLinkNotesTool.parameters

    def __init__(self, client: SecondBrainClient) -> None:
        self._client = client

    def run(self, a: str = "", b: str = "", **kwargs: Any) -> Any:
        try:
            return self._client.distinct(a, b)
        except SecondBrainError as exc:
            return {"error": str(exc)}


class BrainDoneTool(Tool):
    name = "brain_mark_done"
    description = (
        "Mark something in Siris done when he says he's done it (\"I did my run\", \"tick off "
        "the taper\", \"the marathon's finished\"). Give the note's title and the task's text as the brain "
        "shows it to tick one task; leave the task out to mark a whole project finished."
    )
    permission = ToolPermission.SAFE_ACTION
    parameters = {
        "type": "object",
        "properties": {
            "note": {"type": "string", "description": "The note or project's title."},
            "task": {"type": "string", "description": "The task's text; leave out to finish the project."},
        },
        "required": ["note"],
    }

    def __init__(self, client: SecondBrainClient) -> None:
        self._client = client

    def run(self, note: str = "", task: str = "", **kwargs: Any) -> Any:
        try:
            return self._client.done(note, task or None)
        except SecondBrainError as exc:
            return {"error": str(exc)}


class BrainDeleteNoteTool(Tool):
    name = "brain_delete_note"
    description = (
        "Delete a note from Siris when he asks you to (\"delete the note about X\", \"forget "
        "X\", \"remove that from my brain\"). Use the note's exact title (find it with brain_search first). "
        "Links to it in other notes are removed, but the note itself can be brought back with "
        "brain_restore_note, so tell him that. Never use this on your own initiative."
    )
    # Deleting the wrong note on a misheard "forget it" is worse than asking, even though it's undoable.
    permission = ToolPermission.CONFIRM_REQUIRED
    parameters = {
        "type": "object",
        "properties": {"title": {"type": "string", "description": "The exact title of the note to delete."}},
        "required": ["title"],
    }

    def __init__(self, client: SecondBrainClient) -> None:
        self._client = client

    def run(self, title: str = "", **kwargs: Any) -> Any:
        try:
            return self._client.delete_note(title)
        except SecondBrainError as exc:
            return {"error": str(exc)}


class BrainRestoreNoteTool(Tool):
    name = "brain_restore_note"
    description = (
        "Bring back a note that was deleted from Siris (\"undo that\", \"bring back the note "
        "about X\"). It returns where it was and is reconnected to the notes that linked to it."
    )
    permission = ToolPermission.SAFE_ACTION
    parameters = {
        "type": "object",
        "properties": {"title": {"type": "string", "description": "The deleted note's title."}},
        "required": ["title"],
    }

    def __init__(self, client: SecondBrainClient) -> None:
        self._client = client

    def run(self, title: str = "", **kwargs: Any) -> Any:
        try:
            return self._client.restore_note(title)
        except SecondBrainError as exc:
            return {"error": str(exc)}


class BrainLearnTool(Tool):
    name = "brain_learn"
    description = (
        "Add knowledge to Siris, Brad's knowledge base, as a linked note: something durable and worth keeping that "
        "you learned, e.g. a fact, preference, decision, plan, how something works, or a person. Use one "
        "call per idea and title it as the idea or the thing it's about ('Melbourne Marathon 2026 10 km', "
        "'Home Server'). If a note with that title or alias exists, this appends a dated update instead of "
        "duplicating it; it never overwrites. Anything new waits in the brain's inbox (still searchable) "
        "until the nightly sort files it as a new note, merges it into a note on the same topic, or splits it "
        "across the notes it's about when it covers several things. Name "
        "related things in the content: existing notes get linked automatically."
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
        # Something new waits in the inbox, where the nightly sort can merge it into a note on the
        # same topic; adding to a note that already has this title goes straight in.
        item: dict[str, Any] = {"title": title, "content": content, "tags": list(tags or []), "inbox_if_new": True}
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
            # New notes wait in the inbox for the nightly sort; `suggested_path` is the router's guess.
            "path": f"{decision['folder']}/{decision['title']}.md" if decision["action"] == "update" else f"00-Inbox/{title}.md",
            "suggested_path": f"{decision['folder']}/{decision['title']}.md",
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
