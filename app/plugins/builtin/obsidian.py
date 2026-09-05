"""Obsidian tools, via app/obsidian/client.py and the community "Local
REST API" plugin. Registered only when SIRISAI_OBSIDIAN_BASE_URL and
SIRISAI_OBSIDIAN_API_KEY are both set.

Read tools (list/get/search) are READ; writing a note is CONFIRM_REQUIRED
rather than SAFE_ACTION -- unlike a routine restart or a queued search,
overwriting a note's content is genuinely destructive if the model gets
the path wrong or misjudges what should replace what, with no built-in
undo on Obsidian's side. Same reasoning as home_assistant_call_service's
CONFIRM_REQUIRED tier: an unbounded write shouldn't run without a human
approving it first.
"""

from typing import Any

from app.obsidian.client import ObsidianClient
from app.plugins.base import Tool, ToolPermission


class ObsidianListNotesTool(Tool):
    name = "obsidian_list_notes"
    description = (
        "List note/file names in an Obsidian vault, optionally within one folder."
    )
    permission = ToolPermission.READ
    parameters = {
        "type": "object",
        "properties": {
            "folder": {
                "type": "string",
                "description": "Optional folder path to list, e.g. 'Projects'. Omit for the vault root.",
            },
        },
        "required": [],
    }

    def __init__(self, client: ObsidianClient) -> None:
        self._client = client

    def run(self, folder: str | None = None, **kwargs: Any) -> Any:
        return self._client.list_notes(folder=folder)


class ObsidianGetNoteTool(Tool):
    name = "obsidian_get_note"
    description = (
        "Read the full text content of one file in the vault, by its vault-relative path "
        "(e.g. 'Projects/SirisAI.md'). Works for markdown notes and PDFs (text is extracted); "
        "any other file type returns a placeholder rather than unreadable raw bytes."
    )
    permission = ToolPermission.READ
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Vault-relative path to the note.",
            }
        },
        "required": ["path"],
    }

    def __init__(self, client: ObsidianClient) -> None:
        self._client = client

    def run(self, path: str = "", **kwargs: Any) -> Any:
        return self._client.get_note(path)


class ObsidianSearchTool(Tool):
    name = "obsidian_search"
    description = (
        "Search the Obsidian vault for notes containing a given text. Each result gives a "
        "`path` (cite this as the source) and matching `excerpts` -- enough to answer with "
        "provenance, e.g. \"According to Projects/Router-Manual.md: '...'\", rather than an "
        "unsourced claim."
    )
    permission = ToolPermission.READ
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Text to search for."},
            "context_length": {
                "type": "integer",
                "description": "How many characters of context to include around each match. Defaults to 100.",
            },
        },
        "required": ["query"],
    }

    def __init__(self, client: ObsidianClient) -> None:
        self._client = client

    def run(self, query: str = "", context_length: int = 100, **kwargs: Any) -> Any:
        return self._client.search(query, context_length=context_length)


class ObsidianCreateOrUpdateNoteTool(Tool):
    name = "obsidian_create_or_update_note"
    description = (
        "Create a new note, or completely replace an existing note's content, at a vault-relative "
        "path. Replacing an existing note discards whatever was there before -- confirm the path is "
        "right, and whether the intent is really a full replacement, before calling this."
    )
    permission = ToolPermission.CONFIRM_REQUIRED
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Vault-relative path for the note, e.g. 'Projects/SirisAI.md'.",
            },
            "content": {
                "type": "string",
                "description": "The note's full new content (markdown).",
            },
        },
        "required": ["path", "content"],
    }

    def __init__(self, client: ObsidianClient) -> None:
        self._client = client

    def run(self, path: str = "", content: str = "", **kwargs: Any) -> Any:
        self._client.create_or_update_note(path, content)
        return {"written": True, "path": path}
