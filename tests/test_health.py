from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health_ok() -> None:
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["database"] == "ok"


def test_siris_status() -> None:
    response = client.get("/siris/status")
    assert response.status_code == 200
    body = response.json()
    assert body["app_name"] == "SirisAI"
    assert "echo" in body["tools"]
