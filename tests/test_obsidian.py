import httpx
import pytest

from app.obsidian.client import ObsidianClient
from app.plugins.builtin.obsidian import (
    ObsidianCreateOrUpdateNoteTool,
    ObsidianGetNoteTool,
    ObsidianListNotesTool,
    ObsidianSearchTool,
)


def _mock_client(handler) -> ObsidianClient:
    return ObsidianClient(
        base_url="https://obsidian.local:27124",
        api_key="test-key",
        transport=httpx.MockTransport(handler),
    )


def _minimal_pdf_bytes(text: str) -> bytes:
    """A hand-built, minimally valid single-page PDF containing `text` as
    real extractable content -- there's no PDF-writing library in this
    project's dependencies (pypdf itself only reads), so this is the
    smallest fixture that a real PDF parser accepts."""
    content = f"BT /F1 18 Tf 10 100 Td ({text}) Tj ET".encode()
    header = (
        b"%PDF-1.4\n"
        b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
        b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n"
        b"3 0 obj\n<< /Type /Page /Parent 2 0 R /Resources << /Font << /F1 4 0 R >> >> "
        b"/MediaBox [0 0 300 144] /Contents 5 0 R >>\nendobj\n"
        b"4 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\nendobj\n"
    )
    stream_obj = (
        b"5 0 obj\n<< /Length "
        + str(len(content)).encode()
        + b" >>\nstream\n"
        + content
        + b"\nendstream\nendobj\n"
    )
    footer = b"xref\n0 6\n0000000000 65535 f \ntrailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n0\n%%EOF\n"
    return header + stream_obj + footer


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


def test_get_note_extracts_text_from_a_pdf() -> None:
    """The gap this milestone exists for: a manual or contract dropped
    into the vault as a PDF used to come back as garbled/undecodable
    bytes via a plain UTF-8 decode -- now extracted to real text."""
    pdf_bytes = _minimal_pdf_bytes(
        "Restart the router by holding the button for 10 seconds."
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/vault/Manuals/Router.pdf"
        return httpx.Response(
            200, content=pdf_bytes, headers={"Content-Type": "application/pdf"}
        )

    client = _mock_client(handler)
    result = ObsidianGetNoteTool(client).run(path="Manuals/Router.pdf")
    assert "Restart the router by holding the button for 10 seconds." in result
    client.close()


def test_get_note_returns_a_placeholder_for_undecodable_binary_content() -> None:
    """Deliberately doesn't guess at content it can't safely render --
    see the client's own docstring for why a placeholder beats mojibake
    that looks like real text but isn't."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\xff\xfe"
        )

    client = _mock_client(handler)
    result = ObsidianGetNoteTool(client).run(path="Photos/diagram.jpg")
    assert result == "[Cannot display 'Photos/diagram.jpg': not a text file.]"
    client.close()


def test_search_forwards_query_and_context_length() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/search/simple/"
        assert request.url.params["query"] == "homelab"
        assert request.url.params["contextLength"] == "50"
        return httpx.Response(
            200, json=[{"filename": "Notes.md", "score": 1.5, "matches": []}]
        )

    client = _mock_client(handler)
    result = ObsidianSearchTool(client).run(query="homelab", context_length=50)
    assert result == [{"path": "Notes.md", "score": 1.5, "excerpts": []}]
    client.close()


def test_search_normalizes_matches_into_a_citation_shape() -> None:
    """The exact gap this milestone exists for: the plugin's own raw
    shape buries the source filename and each match's text behind nested
    `matches[].context` -- normalized here into a flat, quotable `path` +
    `excerpts` a caller can cite directly."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {
                    "filename": "Projects/Router-Manual.md",
                    "score": 2.1,
                    "matches": [
                        {
                            "match": {"start": 0, "end": 5},
                            "context": "The default admin password is...",
                        },
                        {
                            "match": {"start": 50, "end": 55},
                            "context": "To reset, hold the button for 10s.",
                        },
                    ],
                }
            ],
        )

    client = _mock_client(handler)
    result = ObsidianSearchTool(client).run(query="password")
    assert result == [
        {
            "path": "Projects/Router-Manual.md",
            "score": 2.1,
            "excerpts": [
                "The default admin password is...",
                "To reset, hold the button for 10s.",
            ],
        }
    ]
    client.close()


def test_create_or_update_note_puts_markdown_content() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "PUT"
        assert request.url.path == "/vault/Projects/SirisAI.md"
        assert request.headers["content-type"] == "text/markdown"
        assert request.content == b"# Updated"
        return httpx.Response(204)

    client = _mock_client(handler)
    result = ObsidianCreateOrUpdateNoteTool(client).run(
        path="Projects/SirisAI.md", content="# Updated"
    )
    assert result == {"written": True, "path": "Projects/SirisAI.md"}
    client.close()


def test_create_or_update_note_tool_is_confirm_required() -> None:
    from app.plugins.base import ToolPermission

    assert ObsidianCreateOrUpdateNoteTool.permission is ToolPermission.CONFIRM_REQUIRED


# -- verify(): same "the API call not raising doesn't prove it actually
# happened" gap DockerRestartContainerTool.verify() (38) and the Home
# Assistant sweep (65/76) close elsewhere -- here, a PUT that could
# silently drop a write while the plugin still returns 200.


def test_create_or_update_note_verify_reports_success_when_content_matches() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            return httpx.Response(204)
        assert request.method == "GET"
        assert request.url.path == "/vault/Projects/SirisAI.md"
        return httpx.Response(200, content=b"# Updated")

    client = _mock_client(handler)
    tool = ObsidianCreateOrUpdateNoteTool(client)
    tool.run(path="Projects/SirisAI.md", content="# Updated")

    verification = tool.verify(
        {"path": "Projects/SirisAI.md", "content": "# Updated"}, {}
    )
    assert verification.verified is True
    assert "Projects/SirisAI.md" in verification.detail
    client.close()


def test_create_or_update_note_verify_reports_failure_when_content_does_not_match() -> (
    None
):
    """The exact gap this exists for: a sync conflict or a read-only
    mount can accept the PUT (still 204) without the note's real content
    ever actually changing."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            return httpx.Response(204)
        return httpx.Response(200, content=b"# Old content")

    client = _mock_client(handler)
    tool = ObsidianCreateOrUpdateNoteTool(client)

    verification = tool.verify(
        {"path": "Projects/SirisAI.md", "content": "# Updated"}, {}
    )
    assert verification.verified is False
    assert "Projects/SirisAI.md" in verification.detail
    client.close()


def test_create_or_update_note_verify_raises_not_implemented_for_a_pdf_path() -> None:
    """get_note() extracts PDFs to plain text rather than returning raw
    bytes, so comparing that against the markdown `content` this tool
    wrote would never match even on a fully successful write -- nothing
    honest to check there."""
    tool = ObsidianCreateOrUpdateNoteTool(_mock_client(lambda r: httpx.Response(200)))
    with pytest.raises(NotImplementedError):
        tool.verify({"path": "Manuals/router.pdf", "content": "some text"}, {})


# -- dry_run(): what a full replacement would discard, surfaced before
# confirming rather than after (the same "show what's at risk" reasoning
# UniFiRestartDeviceTool.dry_run() uses for its gateway warning).


def test_create_or_update_note_dry_run_reports_would_create_for_a_new_path() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/vault/Projects/New.md"
        return httpx.Response(404)

    client = _mock_client(handler)
    tool = ObsidianCreateOrUpdateNoteTool(client)

    report = tool.dry_run(path="Projects/New.md", content="# Brand new")
    assert report == {
        "path": "Projects/New.md",
        "exists": False,
        "would_create": True,
        "would_overwrite": False,
    }
    client.close()


def test_create_or_update_note_dry_run_reports_would_overwrite_for_an_existing_path() -> (
    None
):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        return httpx.Response(200, content=b"# Old content")

    client = _mock_client(handler)
    tool = ObsidianCreateOrUpdateNoteTool(client)

    report = tool.dry_run(path="Projects/SirisAI.md", content="# New content")
    assert report == {
        "path": "Projects/SirisAI.md",
        "exists": True,
        "would_create": False,
        "would_overwrite": True,
        "current_content": "# Old content",
        "unchanged": False,
    }
    client.close()


def test_create_or_update_note_dry_run_flags_unchanged_content() -> None:
    client = _mock_client(lambda r: httpx.Response(200, content=b"# Same"))
    tool = ObsidianCreateOrUpdateNoteTool(client)

    report = tool.dry_run(path="Projects/SirisAI.md", content="# Same")
    assert report["unchanged"] is True
    client.close()


def test_create_or_update_note_dry_run_reraises_non_404_errors() -> None:
    client = _mock_client(lambda r: httpx.Response(500))
    tool = ObsidianCreateOrUpdateNoteTool(client)

    with pytest.raises(httpx.HTTPStatusError):
        tool.dry_run(path="Projects/SirisAI.md", content="# New content")
    client.close()


# -- search falls back to file names when the plugin's own search fails (found live:
#    one vault entry that no longer exists on disk made every search a 500)


def test_search_falls_back_to_file_name_matches_when_the_plugin_search_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/search/simple/":
            return httpx.Response(
                500,
                json={
                    "message": "Error encountered while calling Obsidian `prepareSimpleSearch` API.\nError: ENOENT: no such file or directory, open '/NAS/Vaults/Work/Old_DownloadConflict.md'",
                    "errorCode": 50010,
                },
            )
        if request.url.path == "/vault/":
            return httpx.Response(200, json={"files": ["Homelab Notes.md", "Budgets.md", "Projects/"]})
        if request.url.path == "/vault/Projects/":
            return httpx.Response(200, json={"files": ["Homelab Rebuild.md", "Kitchen.md"]})
        raise AssertionError(request.url.path)

    client = _mock_client(handler)
    results = client.search("homelab")
    assert [r["path"] for r in results] == ["Homelab Notes.md", "Projects/Homelab Rebuild.md"]
    assert "file names only" in results[0]["note"] and "Old_DownloadConflict.md" in results[0]["note"]
    client.close()


def test_fallback_search_says_so_when_no_file_name_matches() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/search/simple/":
            return httpx.Response(500, json={"message": "boom"})
        return httpx.Response(200, json={"files": ["Budgets.md"]})

    results = _mock_client(handler).search("zebra")
    assert results[0]["path"] is None and "No file names matched" in results[0]["note"]


def test_a_non_500_search_error_still_raises() -> None:
    import pytest

    client = _mock_client(lambda r: httpx.Response(401, json={"message": "no"}))
    with pytest.raises(httpx.HTTPStatusError):
        client.search("x")
