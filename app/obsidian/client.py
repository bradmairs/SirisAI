"""Thin client for the community "Local REST API" Obsidian plugin
(https://github.com/coddingtonbear/obsidian-local-rest-api) -- Obsidian
itself has no API; this plugin is the de facto standard way to expose one.

HTTPS with a self-signed certificate by default (the plugin's own
default), hence `verify_ssl` -- same pattern as UniFiClient's
self-signed-controller support. Bearer-token auth, the API key shown in
the plugin's own settings pane.
"""

import httpx


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
        response = self._client.get(f"/vault/{path.lstrip('/')}")
        response.raise_for_status()
        return response.text

    def create_or_update_note(self, path: str, content: str) -> None:
        """Fully replaces the note's content if it already exists --
        Obsidian's own plugin semantics, not something this client
        softens. The tool wrapping this (ObsidianCreateOrUpdateNoteTool,
        app/plugins/builtin/obsidian.py) is CONFIRM_REQUIRED for exactly
        this reason: an accidental overwrite silently loses whatever was
        there before, with no built-in undo."""
        response = self._client.put(
            f"/vault/{path.lstrip('/')}", content=content.encode("utf-8"), headers={"Content-Type": "text/markdown"}
        )
        response.raise_for_status()

    def search(self, query: str, context_length: int = 100) -> list[dict]:
        response = self._client.post("/search/simple/", params={"query": query, "contextLength": context_length})
        response.raise_for_status()
        return response.json()

    def close(self) -> None:
        self._client.close()
