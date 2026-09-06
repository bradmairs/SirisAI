"""Unit tests for MemoryStore's semantic recall and write-time dedup
(app/memory/service.py) -- exercised directly against a real (test) SQLite
DB rather than through the LLM/chat layer, same reasoning as
tests/test_cortex.py. `client` is an unused fixture param on tests that
touch the DB, purely to trigger the FastAPI lifespan's table creation
(see tests/test_planner.py for the same pattern) -- MemoryStore opens its
own sessions independent of any HTTP request.
"""

from typing import Any

from fastapi.testclient import TestClient

from app.db.session import SessionLocal
from app.memory.service import MemoryStore


class _FakeEmbeddingClient:
    """Returns a fixed vector per exact content string, set up per test --
    real cosine similarities computed from these, not mocked directly, so
    the actual ranking/threshold logic in MemoryStore is what's tested."""

    def __init__(self, vectors: dict[str, list[float]]) -> None:
        self._vectors = vectors

    def embed(self, text: str) -> list[float]:
        return self._vectors[text]


def _store(vectors: dict[str, list[float]] | None = None) -> MemoryStore:
    client: Any = _FakeEmbeddingClient(vectors) if vectors is not None else None
    return MemoryStore(SessionLocal, embedding_client=client)


def test_recall_falls_back_to_substring_when_no_embedding_client(client: TestClient) -> None:
    store = _store()
    store.remember("favorite homelab distro is Proxmox", user_id="u-substring-1")

    exact = store.recall("Proxmox", user_id="u-substring-1")
    assert [e["content"] for e in exact] == ["favorite homelab distro is Proxmox"]

    paraphrased = store.recall("what hypervisor do I use", user_id="u-substring-1")
    assert paraphrased == []


def test_recall_finds_a_paraphrased_query_via_semantic_similarity(client: TestClient) -> None:
    memory_text = "User prefers homelab routines to run quietly after 22:00."
    query_text = "what did I say about routine timing?"
    vectors = {
        memory_text: [1.0, 0.0],
        query_text: [0.6, 0.8],  # cosine similarity 0.6 -- above the 0.5 floor
    }
    store = _store(vectors)
    store.remember(memory_text, user_id="u-semantic-1")

    results = store.recall(query_text, user_id="u-semantic-1")
    assert [e["content"] for e in results] == [memory_text]


def test_recall_excludes_results_below_the_similarity_floor(client: TestClient) -> None:
    memory_text = "User's home address is 13 Peck Walk, Alphington, Melbourne."
    query_text = "what's the weather like today?"
    vectors = {
        memory_text: [1.0, 0.0],
        query_text: [0.0, 1.0],  # cosine similarity 0.0 -- well below the floor
    }
    store = _store(vectors)
    store.remember(memory_text, user_id="u-semantic-2")

    assert store.recall(query_text, user_id="u-semantic-2") == []


def test_remember_skips_near_duplicate_and_returns_the_existing_entry(client: TestClient) -> None:
    original = "User's name is Brad"
    reworded = "user name is Brad"
    vectors = {
        original: [1.0, 0.0],
        reworded: [0.99, 0.1411],  # cosine similarity ~0.99 -- well above the 0.85 dedup floor
    }
    store = _store(vectors)
    first = store.remember(original, user_id="u-dedup-1")
    second = store.remember(reworded, user_id="u-dedup-1")

    assert second["id"] == first["id"]
    assert store.list_all(user_id="u-dedup-1") == [first]


def test_remember_stores_distinct_content_that_isnt_similar_enough(client: TestClient) -> None:
    name_fact = "User's name is Brad"
    address_fact = "User's home address is 13 Peck Walk, Alphington, Melbourne."
    vectors = {
        name_fact: [1.0, 0.0],
        address_fact: [0.0, 1.0],  # cosine similarity 0.0 -- nowhere near the dedup floor
    }
    store = _store(vectors)
    store.remember(name_fact, user_id="u-dedup-2")
    store.remember(address_fact, user_id="u-dedup-2")

    assert len(store.list_all(user_id="u-dedup-2")) == 2


def test_recall_lazily_backfills_a_memory_saved_before_embeddings_were_configured(client: TestClient) -> None:
    memory_text = "User prefers to be called Brad, not Bradley."
    query_text = "what's my preferred name?"

    # Saved with no embedding client at all -- like every row from before
    # SIRISAI_EMBEDDING_PROVIDER was ever configured.
    unembedded_store = _store()
    saved = unembedded_store.remember(memory_text, user_id="u-backfill-1")
    assert saved["id"] is not None

    # A later store, now with embeddings configured, should still find it.
    vectors = {memory_text: [1.0, 0.0], query_text: [0.7, 0.7141]}
    semantic_store = _store(vectors)
    results = semantic_store.recall(query_text, user_id="u-backfill-1")
    assert [e["content"] for e in results] == [memory_text]


def test_find_duplicate_groups_falls_back_to_exact_content_without_embeddings(client: TestClient) -> None:
    store = _store()
    store.remember("dup text", user_id="u-groups-1")
    store.remember("dup text", user_id="u-groups-1")
    store.remember("unique text", user_id="u-groups-1")

    groups = store.find_duplicate_groups(user_id="u-groups-1")
    assert len(groups) == 1
    assert groups[0]["content"] == "dup text"
    assert len(groups[0]["ids"]) == 2


def test_find_duplicate_groups_catches_pre_existing_reworded_duplicates_via_similarity(client: TestClient) -> None:
    """The real-world case this exists for: rows saved before semantic
    dedup existed (or from before embeddings were configured at all) --
    write-time dedup in remember() only prevents *new* duplicates, so
    two independently-inserted near-duplicate rows are exactly what
    find_duplicate_groups() has to catch retroactively."""
    a, b, c = "User's name is Brad", "user name is Brad", "User's home address is 13 Peck Walk"
    no_embeddings_store = _store()
    no_embeddings_store.remember(a, user_id="u-groups-2")
    no_embeddings_store.remember(b, user_id="u-groups-2")
    no_embeddings_store.remember(c, user_id="u-groups-2")

    vectors = {a: [1.0, 0.0], b: [0.99, 0.1411], c: [0.0, 1.0]}
    semantic_store = _store(vectors)
    groups = semantic_store.find_duplicate_groups(user_id="u-groups-2")

    assert len(groups) == 1
    assert groups[0]["content"] in (a, b)
    assert len(groups[0]["ids"]) == 2


def test_delete_removes_an_owned_memory(client: TestClient) -> None:
    store = _store()
    entry = store.remember("temporary test memory", user_id="u-delete-1")

    assert store.delete(entry["id"], user_id="u-delete-1") is True
    assert store.list_all(user_id="u-delete-1") == []


def test_delete_refuses_a_memory_belonging_to_another_user(client: TestClient) -> None:
    store = _store()
    entry = store.remember("someone else's memory", user_id="u-delete-owner")

    assert store.delete(entry["id"], user_id="u-delete-intruder") is False
    assert store.list_all(user_id="u-delete-owner") == [entry]


def test_delete_returns_false_for_an_unknown_id(client: TestClient) -> None:
    store = _store()
    assert store.delete(999_999, user_id="u-delete-2") is False
