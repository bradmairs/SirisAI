"""6.68, with the repository public: push buttons carry a one-request token, never
the master key; the key is checked in constant time; and with no key set only this
machine gets in."""

from __future__ import annotations

import json
import time
from typing import Any

import pytest
from fastapi import FastAPI, Depends
from fastapi.testclient import TestClient

from app.core import action_tokens
from app.core.auth import require_api_key
from app.core.config import Settings, get_settings


def _app(settings: Settings) -> FastAPI:
    app = FastAPI(dependencies=[Depends(require_api_key)])
    app.dependency_overrides[get_settings] = lambda: settings

    @app.post("/siris/hub/v1/attention/{item}/act")
    def act(item: int, body: dict[str, Any]) -> dict[str, Any]:
        return {"item": item, **body}

    @app.get("/siris/status")
    def status() -> dict[str, str]:
        return {"ok": "yes"}

    return app


def test_a_button_token_opens_only_its_own_request() -> None:
    client = TestClient(_app(Settings(api_key="k" * 32)))
    body = json.dumps({"action": "approve"})
    token = action_tokens.make("k" * 32, "POST", "/siris/hub/v1/attention/7/act", body)
    ok = client.post("/siris/hub/v1/attention/7/act", content=body, headers={"X-Siris-Action": token, "Content-Type": "application/json"})
    assert ok.status_code == 200 and ok.json() == {"item": 7, "action": "approve"}
    other_item = client.post("/siris/hub/v1/attention/8/act", content=body, headers={"X-Siris-Action": token, "Content-Type": "application/json"})
    other_action = client.post("/siris/hub/v1/attention/7/act", content=json.dumps({"action": "reboot"}),
                               headers={"X-Siris-Action": token, "Content-Type": "application/json"})
    other_route = client.get("/siris/status", headers={"X-Siris-Action": token})
    assert other_item.status_code == other_action.status_code == other_route.status_code == 401
    old = action_tokens.make("k" * 32, "POST", "/siris/hub/v1/attention/7/act", body, now=time.time() - 25 * 3600)
    assert client.post("/siris/hub/v1/attention/7/act", content=body, headers={"X-Siris-Action": old, "Content-Type": "application/json"}).status_code == 401
    assert not action_tokens.check("another-key", token, "POST", "/siris/hub/v1/attention/7/act", body.encode())
    assert client.get("/siris/status", headers={"Authorization": "Bearer " + "k" * 32}).status_code == 200
    assert client.get("/siris/status", headers={"Authorization": "Bearer wrong"}).status_code == 401


def _from(app: FastAPI, host: str) -> Any:
    """The app as a client at `host` would reach it."""
    async def asgi(scope: dict[str, Any], receive: Any, send: Any) -> None:
        await app({**scope, "client": (host, 50000)}, receive, send)
    return asgi


def test_with_no_key_only_this_machine_gets_in() -> None:
    open_settings = Settings(api_key=None)
    local = TestClient(_app(open_settings))  # the test client counts as this machine
    assert local.get("/siris/status").status_code == 200
    remote = TestClient(_from(_app(open_settings), "192.168.0.50"))
    blocked = remote.get("/siris/status")
    assert blocked.status_code == 401 and "only this machine" in blocked.json()["detail"]
    allowed = TestClient(_from(_app(Settings(api_key=None, allow_unauthenticated=True)), "192.168.0.50"))
    assert allowed.get("/siris/status").status_code == 200


def test_push_buttons_never_carry_the_key(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.siris import SirisAI
    from app.hub.attention import action
    from app.notify import confirmation

    siris = SirisAI(settings=Settings(api_key="k" * 32, public_base_url="https://siris.example.com"))
    siris.attention.raise_item("sec:1", "confirm", "Reboot nas?", source="tests", actions=[action("approve", "Approve"), action("decline", "Decline")])
    item = next(i for i in siris.attention.list() if i["source"] == "tests")
    buttons = confirmation.item_actions(siris, item) or []
    assert buttons and all("k" * 32 not in json.dumps(b) for b in buttons)
    assert all(b["headers"].get("X-Siris-Action") for b in buttons if b["action"] == "http")


def test_the_ntfy_token_is_sent_to_ntfy() -> None:
    import httpx

    from app.notify.client import NotifyClient

    seen: list[str | None] = []
    client = NotifyClient("https://ntfy.example", "topic", token="tk_abc",
                          transport=httpx.MockTransport(lambda r: (seen.append(r.headers.get("authorization")), httpx.Response(200))[1]))
    client.send("hello")
    assert seen == ["Bearer tk_abc"]
