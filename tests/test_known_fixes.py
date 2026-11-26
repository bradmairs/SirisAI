"""4.30.0: known fixes applied without asking, within a budget, and said."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
from fastapi.testclient import TestClient

from app.core.siris import get_siris
from app.upkeep import known_fixes, rediscover, week_review


def _fresh(siris: Any) -> None:
    for key in (known_fixes.STATE_KEY, week_review.LOG_KEY):
        siris.state.delete(key)


def test_a_service_that_moved_on_this_server_is_switched_and_said(client: TestClient, monkeypatch: Any) -> None:
    from app.core import settings_overrides

    siris = get_siris()
    _fresh(siris)
    monkeypatch.setattr(siris.settings, "hermes_base_url", "http://172.23.0.1:8642")
    monkeypatch.setattr(siris.settings, "server_lan_host", "192.168.0.100")
    monkeypatch.setattr("app.core.health.check_integrations", lambda settings, **kw: [{"name": "Hermes", "ok": False}])
    monkeypatch.setattr(siris, "_docker_client", type("D", (), {"addresses": lambda self: []})())
    monkeypatch.setattr(rediscover, "_answers", lambda url, timeout=2.0: url == "http://192.168.0.100:8642")
    monkeypatch.setattr(settings_overrides, "path", lambda: __import__("pathlib").Path(__import__("tempfile").mkdtemp()) / "s.json")
    found = rediscover.check(siris)
    assert found["moved"][0]["switched"] and siris.settings.hermes_base_url == "http://192.168.0.100:8642"
    note = next(i for i in siris.attention.list("open") if i["key"] == "known_fixes:move:hermes_base_url")
    assert note["severity"] == "info" and "switched over" in note["title"]
    assert any(i["kind"] == "fixed" for i in siris.state.get(week_review.LOG_KEY) or [])


def test_an_address_somewhere_else_still_asks_first(client: TestClient, monkeypatch: Any) -> None:
    assert not rediscover._on_this_server("http://10.9.9.9:8642", [{"name": "hermes", "ips": ["172.20.0.5"]}], ["192.168.0.100"])
    assert rediscover._on_this_server("http://172.20.0.5:8642", [{"name": "hermes", "ips": ["172.20.0.5"]}], [])
    assert rediscover._on_this_server("http://hermes:8642", [{"name": "hermes", "ips": []}], [])


def test_a_stopped_companion_is_restarted_checked_and_said_within_a_budget(client: TestClient, monkeypatch: Any) -> None:
    siris = get_siris()
    _fresh(siris)
    monkeypatch.setattr(siris.settings, "obsidian_base_url", "https://127.0.0.1:27124")
    monkeypatch.setattr(siris.settings, "integration_companions", "Obsidian=obsidian-relay")
    monkeypatch.setattr("app.core.health.check_integrations", lambda settings, **kw: [{"name": "Obsidian", "ok": False}])
    monkeypatch.setattr(siris, "_docker_client", type("D", (), {"addresses": lambda self: [
        {"name": "obsidian-relay", "image": "socat", "state": "exited", "ips": [], "ports": [], "published": []}]})())
    restarted: list[str] = []
    answers = {"now": False}
    real_run = siris.run_tool

    def run_tool(name: str, args: dict[str, Any], **kw: Any) -> Any:
        if name == "docker_restart_container":
            assert kw.get("confirmed") is True
            restarted.append(args["container"])
            answers["now"] = True
            return {"restarted": True}
        return real_run(name, args, **kw)

    monkeypatch.setattr(siris, "run_tool", run_tool)
    monkeypatch.setattr(rediscover, "_answers", lambda url, timeout=2.0: answers["now"])
    assert rediscover.check(siris)["restarts"] == ["obsidian-relay"] and restarted == ["obsidian-relay"]
    note = next(i for i in siris.attention.list("open") if i["key"] == "known_fixes:restart:obsidian-relay")
    assert note["severity"] == "info" and "answers again" in note["title"]
    # the budget: after FIX_BUDGET tries in a day it asks instead
    for _ in range(known_fixes.FIX_BUDGET):
        known_fixes._record(siris, "restart:obsidian-relay", datetime.now(UTC))
    answers["now"] = False
    rediscover.check(siris)
    assert restarted == ["obsidian-relay"]
    assert any(i["kind"] == "confirm" and "obsidian-relay" in i["body"] for i in siris.attention.list("open"))


def test_a_model_in_system_ram_is_reloaded_onto_the_gpu_once_in_six_hours(client: TestClient, monkeypatch: Any) -> None:
    siris = get_siris()
    _fresh(siris)
    monkeypatch.setattr(siris.settings, "llm_provider", "ollama")
    monkeypatch.setattr(siris.settings, "ollama_base_url", "http://ollama:11434")
    state = {"vram": 6_470_000_000}
    calls: list[Any] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/ps":
            return httpx.Response(200, json={"models": [{"name": "llama3.1:8b", "size": 7_230_000_000, "size_vram": state["vram"]}]})
        body = __import__("json").loads(request.content)
        calls.append(body["keep_alive"])
        if body["keep_alive"] != 0:
            state["vram"] = 7_230_000_000
        return httpx.Response(200, json={})

    client_ = httpx.Client(transport=httpx.MockTransport(handler))
    assert known_fixes.gpu_fit(siris, client=client_) == ["llama3.1:8b"]
    assert calls[0] == 0 and calls[1] != 0
    note = next(i for i in siris.attention.list("open") if i["key"] == "known_fixes:gpu:llama3.1:8b")
    assert "0.8 GB in system RAM" in note["title"] and "all on the GPU now" in note["title"]
    state["vram"] = 6_000_000_000
    assert known_fixes.gpu_fit(siris, client=client_) == []  # once in six hours


def test_turned_off_it_asks_first(client: TestClient, monkeypatch: Any) -> None:
    siris = get_siris()
    _fresh(siris)
    monkeypatch.setattr(siris.settings, "auto_fix_known", False)
    assert not known_fixes.allowed(siris, "restart:x")
