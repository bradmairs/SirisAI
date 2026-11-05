import uuid
from typing import Any

from fastapi.testclient import TestClient

from app.core.siris import SirisAI, get_siris
from app.llm.base import LLMResponse, ToolCall
from app.main import app


def test_remember_then_recall_via_endpoints(client: TestClient) -> None:
    # One 32-char hex marker: a dashed uuid splits into 4-char search terms
    # that can match inside another memory's uuid by chance.
    content = f"favorite homelab distro is Proxmox {uuid.uuid4().hex}"

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
    content = f"user is building a k3s cluster {uuid.uuid4().hex}"
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
    marker = uuid.uuid4().hex  # one long search term; dashed uuid chunks can collide
    siris.memory.remember(f"User's favorite color is teal-{marker}")

    results = siris.memory.recall(query="what is my favorite color")
    assert any(f"teal-{marker}" in m["content"] for m in results)


def test_recall_does_not_require_the_entire_query_to_match() -> None:
    """The literal old behavior this replaces: the whole query string had
    to be a substring of the content, which a real question almost never
    is."""
    siris = SirisAI(settings=get_siris().settings)
    marker = uuid.uuid4().hex  # one long search term; dashed uuid chunks can collide
    siris.memory.remember(f"User's name is Brad-{marker}")

    results = siris.memory.recall(query="what's my name")
    assert any(f"Brad-{marker}" in m["content"] for m in results)


def test_recall_falls_back_to_literal_match_when_every_word_is_a_stopword() -> None:
    """A query that's entirely stopwords/too-short words (nothing survives
    filtering) falls back to the old literal-substring behavior rather
    than matching everything indiscriminately."""
    siris = SirisAI(settings=get_siris().settings)
    marker = uuid.uuid4().hex  # one long search term; dashed uuid chunks can collide
    siris.memory.remember(f"unrelated fact {marker}")

    results = siris.memory.recall(query="who am i")
    assert not any(marker in m["content"] for m in results)


def test_recall_still_matches_a_short_exact_query() -> None:
    """Existing behavior for a caller passing an already-short, specific
    query (the common case for a deliberate `recall` tool call) is
    unchanged."""
    siris = SirisAI(settings=get_siris().settings)
    marker = uuid.uuid4().hex  # one long search term; dashed uuid chunks can collide
    siris.memory.remember(f"favorite homelab distro is Proxmox {marker}")

    results = siris.memory.recall(query=marker)
    assert [m["content"] for m in results] == [
        f"favorite homelab distro is Proxmox {marker}"
    ]


def test_delete_memory_removes_it(client: TestClient) -> None:
    content = f"temporary memory to delete {uuid.uuid4().hex}"
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


# -- position: a display-only, 1-based gapless rank distinct from `id`
# (a real, permanent database primary key that must never be renumbered
# -- see MemoryStore's module docstring). Each test uses its own unique
# user_id for full isolation from the shared test-session DB's "default"
# user, since position is computed across everything that user has ever
# remembered.


def test_positions_are_assigned_oldest_first_starting_at_one() -> None:
    siris = SirisAI(settings=get_siris().settings)
    user_id = str(uuid.uuid4())

    first = siris.memory.remember("first memory", user_id=user_id)
    second = siris.memory.remember("second memory", user_id=user_id)
    third = siris.memory.remember("third memory", user_id=user_id)

    assert first["position"] == 1
    assert second["position"] == 2
    assert third["position"] == 3


def test_recall_attaches_the_correct_position_regardless_of_result_order() -> None:
    """recall() with no query returns most-recent-first -- position still
    reflects each entry's oldest-first rank, not its place in that list."""
    siris = SirisAI(settings=get_siris().settings)
    user_id = str(uuid.uuid4())

    siris.memory.remember("oldest", user_id=user_id)
    siris.memory.remember("middle", user_id=user_id)
    siris.memory.remember("newest", user_id=user_id)

    results = siris.memory.recall(user_id=user_id)
    positions_by_content = {r["content"]: r["position"] for r in results}
    assert positions_by_content == {"oldest": 1, "middle": 2, "newest": 3}


def test_positions_close_the_gap_after_a_delete() -> None:
    siris = SirisAI(settings=get_siris().settings)
    user_id = str(uuid.uuid4())

    first = siris.memory.remember("first", user_id=user_id)
    second = siris.memory.remember("second", user_id=user_id)
    third = siris.memory.remember("third", user_id=user_id)

    assert siris.memory.delete(second["id"], user_id=user_id) is True

    results = siris.memory.recall(user_id=user_id)
    positions_by_content = {r["content"]: r["position"] for r in results}
    assert positions_by_content == {"first": 1, "third": 2}

    # A new memory continues the now-shorter sequence rather than
    # reusing the freed number -- still gapless (1..N for the current
    # count), still ascending by age.
    fourth = siris.memory.remember("fourth", user_id=user_id)
    assert fourth["position"] == 3
    # The real database ids are untouched by the delete -- id stays a
    # stable identifier, unlike position.
    assert first["id"] != third["id"]


def test_list_all_includes_positions() -> None:
    siris = SirisAI(settings=get_siris().settings)
    user_id = str(uuid.uuid4())
    siris.memory.remember("only memory", user_id=user_id)

    results = siris.memory.list_all(user_id=user_id)
    assert results == [
        {
            "id": results[0]["id"],
            "position": 1,
            "content": "only memory",
            "created_at": results[0]["created_at"],
            "recall_count": 0,
            "last_recalled_at": None,
        }
    ]


def test_memory_endpoint_exposes_position(client: TestClient) -> None:
    content = f"position via endpoint {uuid.uuid4().hex}"
    created = client.post("/siris/memory", json={"content": content}).json()
    assert isinstance(created["position"], int)

    list_response = client.get("/siris/memory", params={"query": content})
    assert all("position" in e for e in list_response.json())


def test_siris_status_lists_memory_tools(client: TestClient) -> None:
    response = client.get("/siris/status")
    tools = response.json()["tools"]
    assert "remember" in tools
    assert "recall" in tools


# -- delete_memory tool: user-requested, a deliberate reversal of
# MemoryStore.delete()'s original "not exposed as a model tool yet"
# decision


def test_delete_memory_tool_always_registered(client: TestClient) -> None:
    response = client.get("/siris/status")
    assert "delete_memory" in response.json()["tools"]


def test_delete_memory_tool_is_safe_action_permission() -> None:
    from app.plugins.builtin.memory import DeleteMemoryTool

    siris = SirisAI(settings=get_siris().settings)
    tool = DeleteMemoryTool(siris.memory)
    from app.plugins.base import ToolPermission

    assert tool.permission is ToolPermission.SAFE_ACTION


def test_delete_memory_tool_appears_in_memory_capabilities_category(client: TestClient) -> None:
    body = client.get("/siris/capabilities").json()
    assert "delete_memory" in body["memory"]


def test_delete_memory_via_tool_removes_it(client: TestClient) -> None:
    content = f"delete via tool {uuid.uuid4().hex}"
    created = client.post("/siris/memory", json={"content": content}).json()

    response = client.post(
        "/siris/tools/delete_memory/run", json={"arguments": {"memory_id": created["id"]}}
    )
    assert response.status_code == 200
    assert response.json()["tool_call"]["result"] == {"deleted": True, "id": created["id"]}

    list_response = client.get("/siris/memory", params={"query": content})
    assert not any(e["id"] == created["id"] for e in list_response.json())


def test_delete_memory_via_tool_fails_for_an_unknown_id(client: TestClient) -> None:
    response = client.post(
        "/siris/tools/delete_memory/run", json={"arguments": {"memory_id": 999999999}}
    )
    assert response.status_code == 502


def test_chat_can_delete_memory_via_delete_memory_tool(client: TestClient) -> None:
    content = f"forget this via chat {uuid.uuid4().hex}"
    created = client.post("/siris/memory", json={"content": content}).json()

    class _FakeForgettingLLMClient:
        async def generate(
            self,
            messages: list[dict[str, Any]],
            *,
            tools: list[dict[str, Any]] | None = None,
            **kwargs: object,
        ) -> LLMResponse:
            return LLMResponse(
                tool_calls=[ToolCall(name="delete_memory", arguments={"memory_id": created["id"]})]
            )

        async def aclose(self) -> None:
            pass

    fake_siris = get_siris()
    fake_siris.llm = _FakeForgettingLLMClient()  # type: ignore[assignment]
    app.dependency_overrides[get_siris] = lambda: fake_siris
    try:
        chat_response = client.post("/siris/chat", json={"prompt": "please forget that"})
        assert chat_response.status_code == 200
        assert chat_response.json()["tool_call"]["name"] == "delete_memory"
    finally:
        app.dependency_overrides.pop(get_siris, None)

    list_response = client.get("/siris/memory", params={"query": content})
    assert not any(e["id"] == created["id"] for e in list_response.json())


def test_a_memory_can_be_corrected(client: TestClient) -> None:
    """4.4: Brad edits a memory's words from SirisOS; it keeps its id and date."""
    marker = uuid.uuid4().hex
    created = client.post("/siris/memory", json={"content": f"Dom's birthday is 3 March {marker}"}).json()
    r = client.put(f"/siris/memory/{created['id']}", json={"content": f"Dom's birthday is 4 March {marker}"})
    assert r.status_code == 200 and r.json()["id"] == created["id"] and r.json()["created_at"] == created["created_at"]
    assert r.json()["content"] == f"Dom's birthday is 4 March {marker}"
    assert client.put(f"/siris/memory/{created['id']}", json={"content": "  "}).status_code == 422
    assert client.put("/siris/memory/999999999", json={"content": "x"}).status_code == 404
