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


def _with_https_redirect(url: str):
    from app.core.config import Settings, get_settings
    from app.main import app

    app.dependency_overrides[get_settings] = lambda: Settings(https_redirect_url=url)
    return lambda: app.dependency_overrides.pop(get_settings, None)


def test_pages_redirect_to_https_when_configured(client: TestClient) -> None:
    restore = _with_https_redirect("https://192.168.0.100:8443/")
    try:
        for path in ("/", "/hud", "/brain"):
            response = client.get(path, follow_redirects=False)
            assert response.status_code == 302
            assert response.headers["location"] == f"https://192.168.0.100:8443{path}"
        # The one-time ?key= login link keeps working through the redirect.
        response = client.get("/hud?key=abc", follow_redirects=False)
        assert response.headers["location"] == "https://192.168.0.100:8443/hud?key=abc"
    finally:
        restore()


def test_pages_reached_through_the_https_proxy_are_served_not_redirected(client: TestClient) -> None:
    restore = _with_https_redirect("https://192.168.0.100:8443")
    try:
        response = client.get("/hud", headers={"X-Forwarded-Proto": "https"}, follow_redirects=False)
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]
    finally:
        restore()


def test_the_api_and_icons_stay_on_http(client: TestClient) -> None:
    restore = _with_https_redirect("https://192.168.0.100:8443")
    try:
        assert client.get("/health", follow_redirects=False).status_code == 200
        assert client.get("/icon-192.png", follow_redirects=False).status_code == 200
        assert client.get("/manifest.webmanifest", follow_redirects=False).status_code == 200
    finally:
        restore()


def test_no_redirect_when_not_configured(client: TestClient) -> None:
    restore = _with_https_redirect("")
    try:
        assert client.get("/", follow_redirects=False).status_code == 200
    finally:
        restore()
