import uuid
from typing import Any

from fastapi.testclient import TestClient

from app.core.siris import SirisAI, get_siris
from app.llm.base import LLMResponse, ToolCall
from app.main import app


def test_remember_then_recall_via_endpoints(client: TestClient) -> None:
    content = f"favorite homelab distro is Proxmox {uuid.uuid4()}"

    create_response = client.post("/siris/memory", json={"content": content})
    assert create_response.status_code == 200
    created = create_response.json()
    assert created["content"] == content
    assert isinstance(created["id"], int)

    list_response = client.get("/siris/memory", params={"query": content})
    assert list_response.status_code == 200
    entries = list_response.json()
    assert [e["content"] for e in entries] == [content]


class _FakeRememberingLLMClient:
    def __init__(self, content: str) -> None:
        self._content = content

    async def generate(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        **kwargs: object,
    ) -> LLMResponse:
        return LLMResponse(
            tool_calls=[ToolCall(name="remember", arguments={"content": self._content})]
        )

    async def aclose(self) -> None:
        pass


def test_chat_can_write_memory_via_remember_tool(client: TestClient) -> None:
    content = f"user is building a k3s cluster {uuid.uuid4()}"
    fake_siris = get_siris()
    fake_siris.llm = _FakeRememberingLLMClient(content)  # type: ignore[assignment]
    app.dependency_overrides[get_siris] = lambda: fake_siris
    try:
        chat_response = client.post(
            "/siris/chat", json={"prompt": "remember that I'm building a k3s cluster"}
        )
        assert chat_response.status_code == 200
        assert chat_response.json()["tool_call"]["name"] == "remember"
    finally:
        app.dependency_overrides.pop(get_siris, None)

    list_response = client.get("/siris/memory", params={"query": content})
    assert [e["content"] for e in list_response.json()] == [content]


# -- recall() query matching (bug fix): a full natural-language question
# used to have to appear, verbatim, as one substring inside the memory's
# content -- so asking about a fact in different words than it was saved
# in always came back empty. Fixed by matching on any significant word
# from the query rather than the whole string at once.


def test_recall_matches_on_a_significant_word_from_a_multi_word_query() -> None:
    siris = SirisAI(settings=get_siris().settings)
    marker = str(uuid.uuid4())
    siris.memory.remember(f"User's favorite color is teal-{marker}")

    results = siris.memory.recall(query="what is my favorite color")
    assert any(f"teal-{marker}" in m["content"] for m in results)


def test_recall_does_not_require_the_entire_query_to_match() -> None:
    """The literal old behavior this replaces: the whole query string had
    to be a substring of the content, which a real question almost never
    is."""
    siris = SirisAI(settings=get_siris().settings)
    marker = str(uuid.uuid4())
    siris.memory.remember(f"User's name is Brad-{marker}")

    results = siris.memory.recall(query="what's my name")
    assert any(f"Brad-{marker}" in m["content"] for m in results)


def test_recall_falls_back_to_literal_match_when_every_word_is_a_stopword() -> None:
    """A query that's entirely stopwords/too-short words (nothing survives
    filtering) falls back to the old literal-substring behavior rather
    than matching everything indiscriminately."""
    siris = SirisAI(settings=get_siris().settings)
    marker = str(uuid.uuid4())
    siris.memory.remember(f"unrelated fact {marker}")

    results = siris.memory.recall(query="who am i")
    assert not any(marker in m["content"] for m in results)


def test_recall_still_matches_a_short_exact_query() -> None:
    """Existing behavior for a caller passing an already-short, specific
    query (the common case for a deliberate `recall` tool call) is
    unchanged."""
    siris = SirisAI(settings=get_siris().settings)
    marker = str(uuid.uuid4())
    siris.memory.remember(f"favorite homelab distro is Proxmox {marker}")

    results = siris.memory.recall(query=marker)
    assert [m["content"] for m in results] == [
        f"favorite homelab distro is Proxmox {marker}"
    ]


def test_delete_memory_removes_it(client: TestClient) -> None:
    content = f"temporary memory to delete {uuid.uuid4()}"
    created = client.post("/siris/memory", json={"content": content}).json()

    delete_response = client.delete(f"/siris/memory/{created['id']}")
    assert delete_response.status_code == 204

    # Word-based OR matching (recall()'s no-embeddings fallback) means a
    # multi-word query like this one can also match unrelated memories in
    # the shared test-session DB that happen to share a common word (e.g.
    # "memory") -- check the deleted entry specifically isn't among the
    # results, rather than asserting the whole result list is empty.
    list_response = client.get("/siris/memory", params={"query": content})
    assert not any(e["id"] == created["id"] for e in list_response.json())


def test_delete_memory_404s_for_an_unknown_id(client: TestClient) -> None:
    response = client.delete("/siris/memory/999999999")
    assert response.status_code == 404


def test_siris_status_lists_memory_tools(client: TestClient) -> None:
    response = client.get("/siris/status")
    tools = response.json()["tools"]
    assert "remember" in tools
    assert "recall" in tools
