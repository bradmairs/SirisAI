from fastapi.testclient import TestClient


def test_index_serves_html(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "SirisAI" in response.text


def test_index_does_not_require_api_key(client: TestClient) -> None:
    from app.core.config import Settings, get_settings
    from app.main import app

    app.dependency_overrides[get_settings] = lambda: Settings(api_key="secret-key")
    try:
        assert client.get("/").status_code == 200
    finally:
        app.dependency_overrides.pop(get_settings, None)
