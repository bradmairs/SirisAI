"""Thin client for the community "Local REST API" Obsidian plugin
(https://github.com/coddingtonbear/obsidian-local-rest-api) -- Obsidian
itself has no API; this plugin is the de facto standard way to expose one.

HTTPS with a self-signed certificate by default (the plugin's own
default), hence `verify_ssl` -- same pattern as UniFiClient's
self-signed-controller support. Bearer-token auth, the API key shown in
the plugin's own settings pane.
"""

import io

import httpx
from pypdf import PdfReader


class ObsidianClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout: float = 10.0,
        verify_ssl: bool = False,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            headers={"Authorization": f"Bearer {api_key}"},
            verify=verify_ssl,
            transport=transport,
        )

    def list_notes(self, folder: str | None = None) -> list[str]:
        path = f"/vault/{folder.strip('/')}/" if folder else "/vault/"
        response = self._client.get(path)
        response.raise_for_status()
        return response.json().get("files", [])

    def get_note(self, path: str) -> str:
        """Text content of the file at `path`. A vault can hold more than
        markdown notes -- a manual or contract dropped in as a PDF is a
        real, expected case for "document knowledge with citations" -- so
        a `.pdf` path is extracted to plain text via `pypdf` instead of
        being decoded as UTF-8 (which would just be garbage bytes). Any
        other non-text extension is deliberately not guessed at: it comes
        back as a clear placeholder rather than mojibake that looks like
        real content but isn't."""
        response = self._client.get(f"/vault/{path.lstrip('/')}")
        response.raise_for_status()

        if path.lower().endswith(".pdf"):
            reader = PdfReader(io.BytesIO(response.content))
            return "\n\n".join(
                page.extract_text() or "" for page in reader.pages
            ).strip()

        try:
            return response.content.decode("utf-8")
        except UnicodeDecodeError:
            return f"[Cannot display '{path}': not a text file.]"

    def create_or_update_note(self, path: str, content: str) -> None:
        """Fully replaces the note's content if it already exists --
        Obsidian's own plugin semantics, not something this client
        softens. The tool wrapping this (ObsidianCreateOrUpdateNoteTool,
        app/plugins/builtin/obsidian.py) is CONFIRM_REQUIRED for exactly
        this reason: an accidental overwrite silently loses whatever was
        there before, with no built-in undo."""
        response = self._client.put(
            f"/vault/{path.lstrip('/')}",
            content=content.encode("utf-8"),
            headers={"Content-Type": "text/markdown"},
        )
        response.raise_for_status()

    def search(self, query: str, context_length: int = 100) -> list[dict]:
        """Normalized into a consistent citation shape -- `path` +
        `excerpts` -- rather than passing the plugin's own response
        straight through: the raw shape (`filename` + a `matches` list of
        `{match: {start, end}, context}` objects) makes a caller dig
        through nesting just to know which file a hit came from and quote
        it, exactly the kind of provenance friction milestone 5's
        `remember`/`recall` design and the entity/relationship tools were
        built to avoid for memory -- documents deserve the same "know
        where this came from" treatment."""
        response = self._client.post(
            "/search/simple/", params={"query": query, "contextLength": context_length}
        )
        if response.status_code == 500:
            # Found live (2026-09-26): the plugin's search aborts on a single
            # vault entry that no longer exists on disk (a sync-conflict file),
            # so one bad file made every search fail. Fall back to matching
            # file names so there's still an answer, and say why.
            return self._search_by_file_name(query, response)
        response.raise_for_status()
        return [
            {
                "path": result["filename"],
                "score": result.get("score"),
                "excerpts": [match["context"] for match in result.get("matches", [])],
            }
            for result in response.json()
        ]

    def _all_files(self, folder: str | None = None, depth: int = 0) -> list[str]:
        files: list[str] = []
        for entry in self.list_notes(folder):
            path = f"{folder.strip('/')}/{entry}" if folder else entry
            if entry.endswith("/"):
                if depth < 4:
                    try:
                        files.extend(self._all_files(path.rstrip("/"), depth + 1))
                    except httpx.HTTPError:
                        continue
            else:
                files.append(path)
        return files

    def _search_by_file_name(self, query: str, failed: httpx.Response) -> list[dict]:
        try:
            reason = failed.json().get("message", "")
        except ValueError:
            reason = ""
        broken = reason.split("open '")[-1].split("'")[0] if "open '" in reason else None
        note = (
            "Searching note contents failed because the vault has an entry Obsidian can't open"
            + (f" ({broken})" if broken else "")
            + ", so these are matches on file names only."
        )
        needle = query.strip().lower()
        matches = [p for p in self._all_files() if needle in p.lower()]
        return [{"path": p, "score": None, "excerpts": [], "note": note} for p in matches[:20]] or [
            {"path": None, "score": None, "excerpts": [], "note": note + " No file names matched."}
        ]

    def close(self) -> None:
        self._client.close()
