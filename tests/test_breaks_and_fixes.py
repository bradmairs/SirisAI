"""4.29.0: secrets scrubbed everywhere, escalation, services that moved, real failures kept,
arguments checked, and the week's "what broke and what fixed itself"."""

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.core import secrets, tool_args
from app.core.config import Settings
from app.core.siris import get_siris
from app.hub.attention import act


# -- secrets -------------------------------------------------------------------

def test_scrub_masks_configured_secrets_and_url_credentials() -> None:
    secrets.register(Settings(tautulli_api_key="tautkey123456", plex_token="plextoken999"))
    text = ("GET http://t:8181/api/v2?apikey=tautkey123456&cmd=x failed; plex plextoken999; "
            "https://u:pw@host/x?token=abcdef&x=1 Bearer abcdefghijkl")
    out = secrets.scrub(text)
    for leaked in ("tautkey123456", "plextoken999", "token=abcdef", "u:pw@", "abcdefghijkl"):
        assert leaked not in out, leaked
    assert out.count(secrets.MASK) >= 5
    assert secrets.scrub({"a": ["plextoken999"]}) == {"a": [secrets.MASK]}
    assert secrets.scrub("t") == "t"      # short test values aren't treated as secrets


def test_a_tool_error_never_carries_a_secret(client: TestClient, monkeypatch: Any) -> None:
    from app.plugins.base import Tool, ToolPermission

    class Leaky(Tool):
        name = "leaky_lookup"
        description = "test"
        permission = ToolPermission.READ
        parameters = {"type": "object", "properties": {}, "required": []}

        def run(self, **kwargs: Any) -> Any:
            raise RuntimeError("Client error for url http://svc/api?apikey=supersecretvalue1")

    siris = get_siris()
    siris.tools.register(Leaky())
    secrets.add("supersecretvalue1")
    with pytest.raises(Exception) as caught:
        siris.run_tool("leaky_lookup", {})
    assert "supersecretvalue1" not in str(caught.value)
    entry = next(e for e in siris.audit.list_recent(limit=20) if e["tool_name"] == "leaky_lookup")
    assert "supersecretvalue1" not in (entry["error"] or "")
    item = siris.attention.raise_item("test:secret", "alert", "key supersecretvalue1", source="test", body="body supersecretvalue1")
    assert "supersecretvalue1" not in item["title"] + item["body"]


# -- arguments -------------------------------------------------------------------

def test_argument_problems() -> None:
    schema = {"type": "object", "required": ["query"], "properties": {
        "query": {"type": "string"}, "count": {"type": "integer", "minimum": 1, "maximum": 15},
        "kind": {"type": "string", "enum": ["movie", "show"]}, "loud": {"type": "boolean"}, "ids": {"type": "string"}}}
    fixed = tool_args.fit_enums(tool_args.coerce({"query": "x", "count": "5", "kind": "Movie", "ids": ["a", "b"]}, schema), schema)
    assert fixed == {"query": "x", "count": 5, "kind": "movie", "ids": ["a", "b"]} and tool_args.problems(fixed, schema) == []
    wrong = tool_args.problems({"count": "lots", "kind": "song", "loud": "maybe"}, schema)
    assert "'query' is required" in wrong and any("'count' should be integer" in w for w in wrong)
    assert any("one of movie, show" in w for w in wrong) and any("'loud' should be boolean" in w for w in wrong)
    assert tool_args.problems({"query": "x", "count": 50}, schema) == ["'count' must be at most 15"]


def test_a_bad_call_gets_a_clear_message(client: TestClient) -> None:
    siris = get_siris()
    with pytest.raises(ValueError, match="Can't run watch_for: 'what' is required"):
        siris.run_tool("watch_for", {"tool": "get_current_time"})


# -- real failures kept ------------------------------------------------------------

def test_code_bugs_from_real_use_are_kept_and_escalated(client: TestClient, monkeypatch: Any) -> None:
    from app.learning import tool_failures

    siris = get_siris()
    siris.state.delete(tool_failures.STATE_KEY)
    raised: list[tuple[str, bool]] = []
    monkeypatch.setattr("app.upkeep.alerts.raise_alert", lambda s, key, title, body, **kw: raised.append((title, kw.get("issue", False))))
    tool_failures.record(siris, "plex_suggest", {"count": "5"}, "slice indices must be integers (line 12)", "TypeError")
    tool_failures.record(siris, "plex_suggest", {"count": "5"}, "slice indices must be integers (line 40)", "TypeError")
    tool_failures.record(siris, "sonarr_queue", {}, "ConnectError: connection refused", "ConnectError")
    kept = tool_failures.recent(siris)
    bug = next(k for k in kept if k["tool"] == "plex_suggest")
    assert bug["count"] == 2 and bug["code_bug"] and not next(k for k in kept if k["tool"] == "sonarr_queue")["code_bug"]
    assert raised == [("A bug in plex_suggest: TypeError", True)]
    lines = client.get("/siris/learning/tool-failures?format=replay").text.strip().splitlines()
    assert any('"plex_suggest"' in line for line in lines)
    siris.state.delete(tool_failures.STATE_KEY)


# -- self-test escalation -------------------------------------------------------------

def test_a_repeated_self_test_error_is_pushed_then_filed(client: TestClient, monkeypatch: Any) -> None:
    from app.upkeep import self_test

    siris = get_siris()
    siris.state.delete(self_test.STREAK_KEY)
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr("app.upkeep.alerts.raise_alert", lambda s, key, title, body, **kw: calls.append({"title": title, **kw}))
    broken = [{"name": "plex", "question": "What have I watched?", "answer": "(no answer: slice indices must be integers)", "right": False, "truth": "x"}]
    for _ in range(3):
        self_test._escalate(siris, broken, "self-test-x", datetime.now(UTC))
    assert [c["title"] for c in calls] == ["The plex self-test has failed 2 nights running", "The plex self-test has failed 3 nights running"]
    assert calls[0]["push"] and not calls[0]["issue"] and calls[1]["issue"]
    wrong_only = [{**broken[0], "answer": "You watched Love Island", "truth": "Tautulli says Dune"}]
    siris.state.delete(self_test.STREAK_KEY)
    calls.clear()
    for _ in range(3):
        self_test._escalate(siris, wrong_only, "self-test-none", datetime.now(UTC))
    assert calls == []     # a wrong answer without an error stays a feed card
    siris.state.delete(self_test.STREAK_KEY)


# -- services that moved ----------------------------------------------------------------

def test_candidates_for_a_moved_service() -> None:
    from app.upkeep.rediscover import candidates

    containers = [{"name": "hermes", "image": "ghcr.io/x/hermes:1", "state": "running", "ips": ["172.20.0.5"], "ports": [8642], "published": [18642]},
                  {"name": "plex", "image": "plexinc/pms", "state": "running", "ips": ["172.20.0.6"], "ports": [32400], "published": [32400]}]
    found = candidates("http://172.18.0.5:8642", ("hermes",), containers, ["192.168.1.20"])
    assert found[:2] == ["http://172.20.0.5:8642", "http://hermes:8642"]
    assert "http://192.168.1.20:18642" in found and not any("32400" in u for u in found)


def test_a_moved_service_is_offered_and_switched(client: TestClient, monkeypatch: Any) -> None:
    from app.core import settings_overrides
    from app.upkeep import rediscover, week_review

    siris = get_siris()
    monkeypatch.setattr(siris.settings, "auto_fix_known", False)  # 4.30.0: asking first is still there, turned on
    monkeypatch.setattr(siris.settings, "hermes_base_url", "http://172.18.0.5:8642")
    monkeypatch.setattr("app.core.health.check_integrations", lambda settings, **kw: [{"name": "Hermes", "ok": False}])

    class Docker:
        def addresses(self) -> list[dict[str, Any]]:
            return [{"name": "hermes", "image": "hermes", "state": "running", "ips": ["172.20.0.5"], "ports": [8642], "published": []},
                    {"name": "obsidian-relay", "image": "relay", "state": "exited", "ips": [], "ports": [], "published": []}]

    monkeypatch.setattr(siris, "_docker_client", Docker())
    monkeypatch.setattr(rediscover, "_answers", lambda url, timeout=2.0: url == "http://172.20.0.5:8642")
    found = rediscover.check(siris)
    assert found["moved"] == [{"name": "Hermes", "old": "http://172.18.0.5:8642", "new": "http://172.20.0.5:8642"}]
    item = next(i for i in siris.attention.list("open") if i["source"] == "rediscover")
    probe = httpx.Client(base_url="http://172.18.0.5:8642")
    monkeypatch.setattr(siris, "_test_probe", type("P", (), {"_client": probe})(), raising=False)
    monkeypatch.setattr(settings_overrides, "path", lambda: __import__("pathlib").Path(__import__("tempfile").mkdtemp()) / "s.json")
    act(siris, item["id"], "use")
    assert siris.settings.hermes_base_url == "http://172.20.0.5:8642" and str(probe.base_url).rstrip("/") == "http://172.20.0.5:8642"
    assert any(i["kind"] == "moved" for i in siris.state.get(week_review.LOG_KEY) or [])


def test_a_stopped_companion_is_offered_as_a_restart(client: TestClient, monkeypatch: Any) -> None:
    from app.upkeep import rediscover

    siris = get_siris()
    monkeypatch.setattr(siris.settings, "auto_fix_known", False)
    monkeypatch.setattr(siris.settings, "obsidian_base_url", "https://127.0.0.1:27124")
    monkeypatch.setattr(siris.settings, "integration_companions", "Obsidian=obsidian-relay")
    monkeypatch.setattr("app.core.health.check_integrations", lambda settings, **kw: [{"name": "Obsidian", "ok": False}])

    class Docker:
        def addresses(self) -> list[dict[str, Any]]:
            return [{"name": "obsidian-relay", "image": "socat", "state": "exited", "ips": [], "ports": [], "published": []}]

    monkeypatch.setattr(siris, "_docker_client", Docker())
    monkeypatch.setattr(rediscover, "_answers", lambda url, timeout=2.0: False)
    assert rediscover.check(siris)["restarts"] == ["obsidian-relay"]
    item = next(i for i in siris.attention.list("open") if i["kind"] == "confirm" and "obsidian-relay" in i["body"])
    assert item["title"].startswith("Approve")


# -- the week -----------------------------------------------------------------------

def test_the_week_in_review(client: TestClient) -> None:
    from app.upkeep import self_test, week_review
    from app.upkeep.reliability import text

    siris = get_siris()
    now = datetime.now(UTC)
    siris.state.set(self_test.HISTORY_KEY, [{"at": (now - timedelta(days=d)).isoformat(), "passed": 4, "failed": 1 if d < 4 else 0} for d in range(7)])
    siris.state.delete(week_review.LOG_KEY)
    week_review.log(siris, "fixed", "Sonarr (3 h)", now)
    week_review.log(siris, "moved", "Hermes at http://172.20.0.5:8642", now)
    found = week_review.lines(siris, now)
    assert found[0] == "Self-test: 3 of 7 nights all right."
    assert "Broke and came back (fixed by itself or by SirisAI): Sonarr (3 h)." in found and "Found at a new address: Hermes at http://172.20.0.5:8642." in found
    assert "Sonarr (3 h)" in text({"problems": [], "speed": {"replies": 0}, "week": found})
    siris.state.delete(week_review.LOG_KEY)
    siris.state.delete(self_test.HISTORY_KEY)
