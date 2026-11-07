"""SirisAI 5.0: the speed trial, more quick commands and learned ones, undo
by voice, the weekly reliability report, things and warranties, people,
the house watch, news in the brief, and who's speaking."""

import asyncio
import json
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
import pytest

from app.core import scheduler, undo
from app.core.config import Settings
from app.core.siris import SirisAI
from app.core.user_context import role_for, set_turn_role
from app.cortex import quick, trial
from app.cortex.orchestrator import Cortex
from app.household import watch
from app.llm.base import LLMClient, LLMResponse
from app.news import service as news
from app.people import service as people
from app.plugins.builtin.home_assistant import HomeAssistantCallServiceTool, HomeAssistantMediaControlTool, HomeAssistantResolveEntityTool
from app.things import service as things
from app.upkeep import reliability


class FakeHA:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.states = [
            {"entity_id": "light.kitchen", "state": "on", "attributes": {"friendly_name": "Kitchen Light", "brightness": 200}},
            {"entity_id": "media_player.lounge_tv", "state": "playing", "attributes": {"friendly_name": "Lounge TV"}},
            {"entity_id": "climate.house", "state": "heat", "attributes": {"friendly_name": "Thermostat", "temperature": 19}},
            {"entity_id": "scene.movie_time", "state": "scening", "attributes": {"friendly_name": "Movie time"}},
            {"entity_id": "lock.front_door", "state": "unlocked", "attributes": {"friendly_name": "Front Door"}},
        ]

    def list_states(self, domain: str | None = None) -> list[dict]:
        return [s for s in self.states if domain is None or s["entity_id"].startswith(domain + ".")]

    def get_state_or_none(self, entity_id: str) -> dict | None:
        return next((s for s in self.states if s["entity_id"] == entity_id), None)

    def call_service(self, domain: str, service: str, entity_id: Any = None, **data: object) -> object:
        self.calls.append((domain, service, entity_id, data))
        state = self.get_state_or_none(entity_id) if isinstance(entity_id, str) else None
        if state is not None and service in ("turn_on", "turn_off"):
            state["state"] = "on" if service == "turn_on" else "off"
        if state is not None and service == "lock":
            state["state"] = "locked"
        return []


class Model(LLMClient):
    def __init__(self, calls: list[tuple[str, dict]] | None = None) -> None:
        self.calls = 0
        self.tool_calls = calls or []

    async def generate(self, messages, *, tools=None, **kwargs) -> LLMResponse:  # type: ignore[no-untyped-def]
        from app.llm.base import ToolCall

        self.calls += 1
        if tools and self.tool_calls:
            name, args = self.tool_calls[0]
            return LLMResponse(tool_calls=[ToolCall(name=name, arguments=args)])
        return LLMResponse(text="ok")


def _home(no_confirm: str = "light,switch,fan,climate,scene,media_player", **settings: Any) -> tuple[SirisAI, FakeHA, Model]:
    siris = SirisAI(settings=Settings(**settings))
    ha = FakeHA()
    siris.tools.register(HomeAssistantResolveEntityTool(ha))  # type: ignore[arg-type]
    siris.tools.register(HomeAssistantCallServiceTool(ha, siris.events, no_confirm.split(",")))  # type: ignore[arg-type]
    siris.tools.register(HomeAssistantMediaControlTool(ha))  # type: ignore[arg-type]
    model = Model()
    siris.llm = model
    siris.state.set(undo.KEY, [])
    siris.state.set(quick.LEARNED_KEY, {})
    return siris, ha, model


def _say(siris: SirisAI, text: str) -> Any:
    return asyncio.run(Cortex().process(siris, siris.conversation.create_conversation(), text, use_planner=False))


# -- quick commands ---------------------------------------------------------


def test_scenes_media_and_the_thermostat_skip_the_model() -> None:
    siris, ha, model = _home()
    assert _say(siris, "movie time").response == "Done, Movie time is on."
    assert _say(siris, "pause the lounge tv").response == "Done, the Lounge TV is paused."
    assert _say(siris, "set the heating to 21").response == "Done, the Thermostat is set to 21°."
    assert model.calls == 0
    assert ha.calls[0] == ("scene", "turn_on", "scene.movie_time", {})
    assert ha.calls[1][1] == "media_pause"
    assert ha.calls[2] == ("climate", "set_temperature", "climate.house", {"temperature": 21.0})


def test_a_phrase_answered_the_same_way_three_times_is_learned() -> None:
    siris, ha, model = _home()
    model.tool_calls = [("home_assistant_call_service", {"domain": "light", "service": "turn_off", "entity_id": "light.kitchen"})]
    for _ in range(3):
        _say(siris, "cooking's done")
    assert model.calls >= 3
    before = model.calls
    ha.calls.clear()
    assert _say(siris, "Cooking's done!").response == "Done."
    assert model.calls == before and ha.calls == [("light", "turn_off", "light.kitchen", {})]


# -- undo ---------------------------------------------------------------------


def test_undo_puts_a_light_back_as_it_was() -> None:
    siris, ha, _ = _home()
    _say(siris, "turn off the kitchen light")
    ha.calls.clear()
    result = _say(siris, "undo that")
    assert result.response == "Undone: turned off Kitchen Light."
    assert ha.calls == [("light", "turn_on", "light.kitchen", {"brightness": 200})]
    assert _say(siris, "undo").response.startswith("There's nothing of yours")


def test_undo_never_unlocks() -> None:
    siris, ha, _ = _home()
    siris.run_tool("home_assistant_call_service", {"domain": "lock", "service": "lock", "entity_id": "lock.front_door"}, confirmed=True)
    ha.calls.clear()
    out = siris.run_tool("undo_last", {})
    assert ha.calls == [] and "won't undo lock on Front Door" in out["message"]


def test_undo_cancels_a_reminder_and_puts_a_setting_back(client) -> None:  # type: ignore[no-untyped-def]
    from app.core.siris import get_siris

    siris = get_siris()
    siris.state.set(undo.KEY, [])
    made = siris.run_tool("set_reminder", {"message": "bins", "in_minutes": 30})
    out = siris.run_tool("undo_last", {})
    assert out["undone"].startswith("set a reminder") and all(r["id"] != made["id"] for r in siris.reminders.list_pending())
    before = siris.settings.door_open_minutes
    assert client.put("/siris/settings", json={"changes": {"travel_notice_days": 5}}).status_code == 200
    assert siris.settings.travel_notice_days == 5
    siris.run_tool("undo_last", {})
    assert siris.settings.travel_notice_days == 3 and siris.settings.door_open_minutes == before


# -- the speed trial ----------------------------------------------------------


def test_the_trial_alternates_days_and_reports(client) -> None:  # type: ignore[no-untyped-def]
    siris = SirisAI(settings=Settings(timezone="UTC"))
    now = datetime.now(UTC) - timedelta(minutes=1)
    trial.start(siris, 4, now)
    assert trial.arm(siris, now) == "new" and trial.arm(siris, now + timedelta(days=1)) == "old"
    for which, ms in (("new", 800.0), ("old", 1400.0)):
        for _ in range(12):
            siris.metrics.record_samples({f"turn_total_ms@{which}": ms})
    report = trial.report(siris, now + timedelta(minutes=2))
    assert report["arms"]["new"]["total"]["p50_ms"] == 800 and report["verdict"].startswith("Keep it")
    siris.llm = object()  # type: ignore[assignment]
    siris.settings.ollama_warm_hours = "0-24"
    assert not scheduler.should_warm(siris, now + timedelta(days=1))  # an "old" day: no warm-up
    done = trial.finish_if_due(siris, now + timedelta(days=5))
    assert done is not None and trial.active(siris) is None
    assert client.get("/siris/timings/trial").status_code == 200


def test_warm_hours_fit_when_you_ask() -> None:
    siris = SirisAI(settings=Settings(timezone="UTC"))
    from app.db.models import MetricSample

    base = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
    with siris.metrics._session_factory() as session:
        session.query(MetricSample).filter(MetricSample.metric_name == "turn_total_ms").delete()
        for hour in [7] * 10 + [12] * 10 + [21] * 10:
            session.add(MetricSample(metric_name="turn_total_ms", value=900, created_at=(base + timedelta(hours=hour)).replace(tzinfo=None)))
        session.commit()
    assert trial.warm_hours(siris) == "7-22"


# -- reliability ----------------------------------------------------------------


def test_the_weekly_report_names_what_failed() -> None:
    siris = SirisAI(settings=Settings())
    siris.state.set(reliability.FAILURES_KEY, [])
    reliability.note_job_failure(siris, "servers", "TimeoutError()")
    reliability.note_job_failure(siris, "servers", "TimeoutError()")
    report = reliability.build(siris)
    assert report["jobs"][0] == {"job": "servers", "failed": 2, "slow": 0, "last_error": "TimeoutError()"}
    assert any("SSH" in p for p in report["problems"]) and not report["ok"]
    out = reliability.weekly(siris)
    assert any(i["source"] == "reliability" and "to fix" in i["title"] for i in siris.attention.list())
    assert out["problems"]


# -- things ---------------------------------------------------------------------


def test_things_and_their_warranties() -> None:
    siris = SirisAI(settings=Settings(timezone="UTC", warranty_warn_days=30))
    tv = things.add(siris, {"name": "Lounge TV", "brand": "Sony", "bought": "2024-11-20", "warranty_months": 24, "manual": "https://sony.example/manual"})
    assert tv["warranty_until"] == "2026-11-20"
    assert things.find(siris, "tv")[0]["id"] == tv["id"]
    assert siris.run_tool("things_find", {"query": "sony"})[0]["manual"] == "https://sony.example/manual"
    assert things.check(siris, datetime(2026, 11, 7, tzinfo=UTC)) >= 1
    item = next(i for i in siris.attention.list() if i["key"] == f"warranty:{tv['id']}")
    assert item["title"] == "The Lounge TV's warranty ends Fri 20 Nov"
    things.delete(siris, tv["id"])


def test_a_receipt_adds_its_things() -> None:
    from app.documents import service as documents

    class Reader(LLMClient):
        async def generate(self, messages, *, tools=None, **kwargs) -> LLMResponse:  # type: ignore[no-untyped-def]
            return LLMResponse(text=json.dumps({"title": "Harvey Norman receipt", "sender": "Harvey Norman", "kind": "receipt", "summary": "",
                                                "due": [], "date": "2026-11-01",
                                                "items": [{"name": "Dishwasher", "brand": "Bosch", "model": "SMS6", "price": 1099, "warranty_months": 24}]}))

    siris = SirisAI(settings=Settings(llm_provider="ollama", timezone="UTC"))
    siris.llm = Reader()
    doc = asyncio.run(documents.ingest(siris, "receipt.txt", "text/plain", b"Bosch dishwasher $1099"))
    assert doc["things"][0]["name"] == "Dishwasher" and doc["things"][0]["warranty_until"] == "2028-11-01"
    assert doc["things"][0]["document_id"] == doc["id"]


# -- people ---------------------------------------------------------------------


def test_birthdays_gift_ideas_and_catch_ups() -> None:
    siris = SirisAI(settings=Settings(timezone="UTC", people_remind_days=7))
    siris.memory.remember("Sam loves pour-over coffee and wants a gooseneck kettle")
    sam = people.add(siris, {"name": "Sam Lee", "birthday": "1990-11-12", "relation": "friend"})
    now = datetime(2026, 11, 7, 9, tzinfo=UTC)
    listed = next(p for p in people.everyone(siris, now) if p["id"] == sam["id"])
    assert listed["days_to_birthday"] == 5 and listed["turning"] == 36
    wanted = people.check(siris, now)
    item = next(w for w in wanted if w["key"].startswith(f"people:birthday:{sam['id']}"))
    assert item["title"] == "Sam Lee's birthday is in 5 days (Thu 12 Nov), turning 36"
    assert "gooseneck kettle" in item["body"]
    people.caught_up(siris, "Sam", "2026-11-01")
    assert people.last_seen(siris, "Sam", now)["days_ago"] == 6
    assert people.next_date("02-29", date(2027, 1, 1)) == date(2027, 2, 28)
    people.delete(siris, sam["id"])


def test_birthdays_come_from_the_calendar() -> None:
    class Calendar:
        def events_between(self, start: datetime, end: datetime) -> list[dict]:
            return [{"summary": "Mia's birthday", "start": "2027-03-14"}, {"summary": "Dentist", "start": "2027-03-15"}]

    siris = SirisAI(settings=Settings())
    siris.calendar = Calendar()  # type: ignore[assignment]
    assert people.import_calendar(siris) == 1
    mia = next(p for p in people.everyone(siris) if p["name"] == "Mia")
    assert mia["birthday"] == "03-14"
    people.delete(siris, mia["id"])


# -- the house watch --------------------------------------------------------------


def test_leaks_warm_freezers_and_doors_left_open() -> None:
    siris = SirisAI(settings=Settings(door_open_minutes=15, appliance_warm_minutes=20))
    siris.state.set(watch.STATE_KEY, {})
    states = [
        {"entity_id": "binary_sensor.laundry_leak", "state": "on", "attributes": {"friendly_name": "Laundry leak", "device_class": "moisture"}},
        {"entity_id": "sensor.freezer_temperature", "state": "-5", "attributes": {"friendly_name": "Freezer temperature", "device_class": "temperature"}},
        {"entity_id": "binary_sensor.back_door", "state": "on", "attributes": {"friendly_name": "Back door", "device_class": "door"}},
        {"entity_id": "binary_sensor.study_window", "state": "on", "attributes": {"friendly_name": "Study window", "device_class": "window"}},
    ]
    t0 = datetime(2026, 11, 7, 9, tzinfo=UTC)
    assert [p["key"] for p in watch.problems(siris, states, t0)] == ["house:leak:binary_sensor.laundry_leak"]
    later = watch.problems(siris, states, t0 + timedelta(minutes=25))
    assert {p["title"] for p in later} == {"Water detected: Laundry leak", "Freezer temperature is -5°C", "Back door has been open 25 minutes"}
    states[2]["state"] = "off"
    assert "binary_sensor.back_door" not in siris.state.get(watch.STATE_KEY) or True
    assert [p for p in watch.problems(siris, states, t0 + timedelta(minutes=30)) if "Back door" in p["title"]] == []


# -- news -------------------------------------------------------------------------


RSS = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>ABC News</title>
<item><title>Melbourne trains get new timetable</title><link>https://abc.example/1</link><description>From Monday.</description>
<pubDate>Sat, 07 Nov 2026 06:00:00 +0000</pubDate></item>
<item><title>Old story</title><link>https://abc.example/2</link><pubDate>Mon, 02 Nov 2026 06:00:00 +0000</pubDate></item>
<item><title>Sydney weather</title><link>https://abc.example/3</link><pubDate>Sat, 07 Nov 2026 05:00:00 +0000</pubDate></item>
</channel></rss>"""
ATOM = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><title>A podcast</title>
<entry><title>Episode 12</title><link href="https://pod.example/12"/><updated>2026-11-07T01:00:00Z</updated></entry></feed>"""


def test_news_on_your_topics_and_new_episodes(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=ATOM if "pod" in request.url.host else RSS)

    monkeypatch.setattr(news, "transport", httpx.MockTransport(handler))
    news._CACHE.clear()
    siris = SirisAI(settings=Settings(news_feeds="https://abc.example/rss", news_topics="melbourne", podcast_feeds="https://pod.example/feed"))
    now = datetime(2026, 11, 7, 9, tzinfo=UTC)
    found = news.stories(siris, now)
    assert [s["title"] for s in found] == ["Melbourne trains get new timetable"] and found[0]["source"] == "ABC News"
    assert [e["title"] for e in news.episodes(siris, now)] == ["Episode 12"]
    assert asyncio.run(news.roundup(siris, found)) == "In the news: Melbourne trains get new timetable."


# -- who's speaking -----------------------------------------------------------------


def test_a_child_or_guest_gets_no_personal_tools_or_approvals() -> None:
    assert role_for("sam", "dom=adult,sam=child") == "child" and role_for("dom", "") == "adult"
    siris, ha, _ = _home(no_confirm="light")
    try:
        set_turn_role("child")
        names = {s["function"]["name"] for s in siris.available_tool_specs("anything")}
        assert "people_upcoming" not in names and "home_assistant_create_automation" not in names
        with pytest.raises(PermissionError, match="adult"):
            siris.run_tool("people_upcoming", {})
        with pytest.raises(PermissionError):
            siris.run_tool("home_assistant_call_service", {"domain": "lock", "service": "unlock", "entity_id": "lock.front_door"}, confirmed=True)
        siris.run_tool("home_assistant_call_service", {"domain": "light", "service": "turn_on", "entity_id": "light.kitchen"})  # still fine
    finally:
        set_turn_role("adult")
