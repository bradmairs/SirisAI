import httpx

from app.obsidian.client import ObsidianClient
from app.plugins.builtin.obsidian import (
    ObsidianCreateOrUpdateNoteTool,
    ObsidianGetNoteTool,
    ObsidianListNotesTool,
    ObsidianSearchTool,
)


def _mock_client(handler) -> ObsidianClient:
    return ObsidianClient(base_url="https://obsidian.local:27124", api_key="test-key", transport=httpx.MockTransport(handler))


def test_list_notes_at_vault_root() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/vault/"
        assert request.headers["authorization"] == "Bearer test-key"
        return httpx.Response(200, json={"files": ["Notes.md", "Projects/"]})

    client = _mock_client(handler)
    result = ObsidianListNotesTool(client).run()
    assert result == ["Notes.md", "Projects/"]
    client.close()


def test_list_notes_in_a_folder() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/vault/Projects/"
        return httpx.Response(200, json={"files": ["SirisAI.md"]})

    client = _mock_client(handler)
    result = ObsidianListNotesTool(client).run(folder="Projects")
    assert result == ["SirisAI.md"]
    client.close()


def test_get_note_returns_raw_content() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/vault/Projects/SirisAI.md"
        return httpx.Response(200, text="# SirisAI\n\nA Jarvis-style assistant.")

    client = _mock_client(handler)
    result = ObsidianGetNoteTool(client).run(path="Projects/SirisAI.md")
    assert result == "# SirisAI\n\nA Jarvis-style assistant."
    client.close()


def test_search_forwards_query_and_context_length() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/search/simple/"
        assert request.url.params["query"] == "homelab"
        assert request.url.params["contextLength"] == "50"
        return httpx.Response(200, json=[{"filename": "Notes.md", "matches": []}])

    client = _mock_client(handler)
    result = ObsidianSearchTool(client).run(query="homelab", context_length=50)
    assert result == [{"filename": "Notes.md", "matches": []}]
    client.close()


def test_create_or_update_note_puts_markdown_content() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "PUT"
        assert request.url.path == "/vault/Projects/SirisAI.md"
        assert request.headers["content-type"] == "text/markdown"
        assert request.content == b"# Updated"
        return httpx.Response(204)

    client = _mock_client(handler)
    result = ObsidianCreateOrUpdateNoteTool(client).run(path="Projects/SirisAI.md", content="# Updated")
    assert result == {"written": True, "path": "Projects/SirisAI.md"}
    client.close()


def test_create_or_update_note_tool_is_confirm_required() -> None:
    from app.plugins.base import ToolPermission

    assert ObsidianCreateOrUpdateNoteTool.permission is ToolPermission.CONFIRM_REQUIRED
