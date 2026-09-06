import json

import httpx

from app.llm.embeddings import OllamaEmbeddingClient, cosine_similarity


def test_embed_posts_to_the_embed_endpoint_and_returns_the_vector() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/embed"
        assert json.loads(request.content) == {"model": "nomic-embed-text", "input": "hello"}
        return httpx.Response(200, json={"embeddings": [[0.1, 0.2, 0.3]]})

    client = OllamaEmbeddingClient(
        base_url="http://localhost:11434", model="nomic-embed-text", transport=httpx.MockTransport(handler)
    )
    assert client.embed("hello") == [0.1, 0.2, 0.3]
    client.close()


def test_cosine_similarity_of_identical_vectors_is_one() -> None:
    assert cosine_similarity([1.0, 2.0, 3.0], [1.0, 2.0, 3.0]) == 1.0


def test_cosine_similarity_of_orthogonal_vectors_is_zero() -> None:
    assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == 0.0


def test_cosine_similarity_of_opposite_vectors_is_negative_one() -> None:
    assert cosine_similarity([1.0, 0.0], [-1.0, 0.0]) == -1.0


def test_cosine_similarity_handles_a_zero_vector_without_dividing_by_zero() -> None:
    assert cosine_similarity([0.0, 0.0], [1.0, 1.0]) == 0.0
