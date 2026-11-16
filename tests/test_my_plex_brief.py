"""6.76: the morning brief's Plex section is only what you watched."""

from typing import Any

import httpx

from app.plugins.builtin.tautulli import TautulliRecentHistoryTool
from app.tautulli.client import TautulliClient

USERS = [
    {"user_id": 1, "username": "plexowner", "friendly_name": "Brad", "email": "owner@example.com", "is_admin": 1},
    {"user_id": 2, "username": "brad_second", "friendly_name": "Living room", "email": "second@example.com", "is_admin": 0},
    {"user_id": 3, "username": "flatmate", "friendly_name": "Sam", "email": "sam@example.com", "is_admin": 0},
]
HISTORY = {
    1: [{"full_title": "The Bear - S03E01", "user": "Brad", "date": 300}],
    2: [{"full_title": "Dune: Part Two", "user": "Living room", "date": 400}],
    3: [{"full_title": "Love Island", "user": "Sam", "date": 500}],
}


def _client(seen: list[dict[str, str]]) -> TautulliClient:
    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        seen.append(params)
        if params["cmd"] == "get_users":
            return httpx.Response(200, json={"response": {"data": USERS}})
        if "user_id" in params:
            rows = HISTORY[int(params["user_id"])]
        else:
            rows = [r for rows in HISTORY.values() for r in rows]
        return httpx.Response(200, json={"response": {"data": {"data": rows}}})

    return TautulliClient(base_url="http://t", api_key="k", transport=httpx.MockTransport(handler))


def test_me_is_the_server_owner_by_default() -> None:
    seen: list[dict[str, str]] = []
    mine = TautulliRecentHistoryTool(_client(seen)).run(user="me", length=5)
    assert [r["title"] for r in mine["history"]] == ["The Bear - S03E01"]
    assert seen[-1]["user_id"] == "1"


def test_several_accounts_by_name_display_name_or_email() -> None:
    seen: list[dict[str, str]] = []
    tool = TautulliRecentHistoryTool(_client(seen), lambda: "brad, Living room, nobody")
    mine = tool.run(user="me", length=5)
    assert [r["title"] for r in mine["history"]] == ["Dune: Part Two", "The Bear - S03E01"]  # newest first
    assert "Love Island" not in str(mine)
    by_email = TautulliRecentHistoryTool(_client(seen), lambda: "SECOND").run(user="me")
    assert [r["title"] for r in by_email["history"]] == ["Dune: Part Two"]


def test_the_setting_is_read_when_asked() -> None:
    seen: list[dict[str, str]] = []
    name = {"value": ""}
    tool = TautulliRecentHistoryTool(_client(seen), lambda: name["value"])
    assert tool.run(user="me")["history"][0]["title"] == "The Bear - S03E01"
    name["value"] = "flatmate"
    assert tool.run(user="me")["history"][0]["title"] == "Love Island"


def test_the_brief_asks_for_only_mine() -> None:
    from app.skills.builtin.daily_briefing import DailyBriefingSkill
    from tests.test_daily_briefing import _FakeSirisForBriefing

    siris = _FakeSirisForBriefing(has_tautulli=True)
    calls: list[dict[str, Any]] = []
    original = siris.run_tool

    def run_tool(name: str, arguments: dict[str, Any], **kwargs: Any) -> Any:
        if name == "tautulli_recent_history":
            calls.append(arguments)
            return {"user": "Brad", "history": [{"title": "The Bear - S03E01", "user": "Brad", "date": 300}] * 2}
        return original(name, arguments, **kwargs)

    siris.run_tool = run_tool  # type: ignore[method-assign]
    briefing = DailyBriefingSkill().run(siris)
    assert calls == [{"length": 5, "user": "me"}]
    assert "You recently watched: The Bear - S03E01." in briefing["summary"]  # once, not twice
    assert "(Brad)" not in briefing["summary"]
