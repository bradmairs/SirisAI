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
        response.raise_for_status()
        return [
            {
                "path": result["filename"],
                "score": result.get("score"),
                "excerpts": [match["context"] for match in result.get("matches", [])],
            }
            for result in response.json()
        ]

    def close(self) -> None:
        self._client.close()
