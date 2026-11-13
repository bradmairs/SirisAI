"""6.58: backups restored, disks forecast, update notes, device costs, the network
map, briefing order, state reminders, undo history, prefetching, the weekly
review and SirisAI's health page."""

from __future__ import annotations

import json
import time
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.siris import SirisAI
from app.db.models import AttentionItem
from app.db.session import SessionLocal

NOW = datetime(2026, 11, 13, 1, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _clean(client: TestClient):  # type: ignore[no-untyped-def]
    with SessionLocal() as session:
        session.query(AttentionItem).delete()
        session.commit()
    yield


def _feed(siris: SirisAI, source: str) -> dict[str, dict[str, Any]]:
    return {i["title"]: i for i in siris.attention.list() if i["source"] == source}


# -- 1. backups restored and compared ---------------------------------------------------


def test_a_backup_test_restores_and_compares() -> None:
    from app.homelab import backups

    ok = backups.parse_test("##from s1\n##file /t/.zfs/snapshot/s1/a\n##bytes 10\n##ok read it all\n##restored abc\n##live same\n")
    assert ok["ok"] and ok["restored"] and ok["live"] == "same"
    bad_copy = backups.parse_test("##file /x/a\n##ok read it all\n##restorefailed\n")
    assert not bad_copy["ok"] and "scratch folder" in bad_copy["why"]
    rotten = backups.parse_test("##file /x/a\n##ok read it all\n##restored abc\n##live differs\n")
    assert not rotten["ok"] and "differs from the live file" in rotten["why"]
    assert backups.parse_test("##file /x/a\n##ok read it all\n##restored abc\n##live changed\n")["ok"]
    cmd = backups.test_command(backups.parse("nas:zfs:tank/data")[0])
    assert "mktemp -d" in cmd and "sha256sum" in cmd and 'rm -rf "$tmp"' in cmd and "-size -2048M" in cmd


# -- 2. when disks fill ------------------------------------------------------------------


def test_a_filling_disk_is_forecast() -> None:
    from app.servers import forecast

    siris = SirisAI(settings=Settings(disk_forecast_days=30))
    siris.state.delete(forecast.STATE_KEY)
    total = 1000 * 1024**2  # 1000 GB in kB
    for i in range(10):
        day = NOW - timedelta(days=9 - i)
        used = (800 + 5 * i) * 1024**2  # 5 GB a day
        stdout = f"##dfk\nFilesystem 1024-blocks Used Available Capacity Mounted\n/dev/sdb1 {total} {used} {total - used} 80% /mnt/media\n"
        forecast.record(siris, "nas", {"disks": [{"mount": "/mnt/media", "size": "1000G", "free": "1G", "used_percent": 85}]}, stdout, day)
    found = forecast.forecasts(siris)[0]
    assert found["days_to_full"] == pytest.approx(31.0, abs=0.5) and found["per_day_kb"] == pytest.approx(5 * 1024**2)
    assert forecast.wanted(siris, 90) == []  # 31 days: just outside 30
    siris.settings.disk_forecast_days = 45
    item = forecast.wanted(siris, 90)[0]
    assert item["title"] == "/mnt/media on nas fills in about 4 weeks at this rate" and "5.0 GB a day" in item["body"]
    assert "/mnt/media on nas: 84% used, 5.0 GB a day" in forecast.text(siris)
    assert forecast._kb("3.6T") == pytest.approx(3.6 * 1024**3) and forecast.fit([["2026-11-01", 1, 2]]) is None


# -- 3. what an update changes -----------------------------------------------------------


def test_update_notes_flag_breaking_changes() -> None:
    from app.homelab import release_notes

    releases = [
        {"tag_name": "v4.1.0", "name": "4.1.0", "html_url": "https://github.com/acme/app/releases/v4.1.0", "body": "BREAKING: config moved", "draft": False, "prerelease": False},
        {"tag_name": "v4.0.2", "name": "4.0.2", "html_url": "https://github.com/acme/app/releases/v4.0.2", "body": "fixes", "draft": False, "prerelease": False},
        {"tag_name": "v4.0.1", "name": "4.0.1", "html_url": "u", "body": "", "draft": False, "prerelease": False},
    ]
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, json=releases)

    siris = SirisAI(settings=Settings(container_release_notes=True))
    siris.state.delete(release_notes.CACHE_KEY)
    labels = {"org.opencontainers.image.source": "https://github.com/acme/app", "org.opencontainers.image.version": "4.0.1"}
    notes = release_notes.summarise(siris, labels, transport=httpx.MockTransport(handler))
    assert [r["tag"] for r in notes["releases"]] == ["v4.1.0", "v4.0.2"] and notes["breaking"]
    assert release_notes.summarise(siris, labels, transport=httpx.MockTransport(handler))["repo"] == "acme/app" and len(seen) == 1  # kept a day
    line = release_notes.line("app", notes)
    assert "v4.1.0, v4.0.2" in line and "Check first: v4.1.0 mentions a breaking change" in line
    assert release_notes.summarise(siris, {}) is None
    assert release_notes.repo_of({"org.opencontainers.image.source": "git@github.com:acme/app.git"}) == "acme/app"


# -- 4. what each device costs -----------------------------------------------------------


class FakeHA:
    def __init__(self, history: dict[str, list[tuple[datetime, str]]], states: list[dict[str, Any]] | None = None) -> None:
        self.history = history
        self.states = states or [
            {"entity_id": "sensor.pool_pump_energy", "state": "0", "attributes": {"friendly_name": "Pool pump", "device_class": "energy", "unit_of_measurement": "kWh", "state_class": "total_increasing"}},
            {"entity_id": "sensor.office_pc_power", "state": "0", "attributes": {"friendly_name": "Office PC", "device_class": "power", "unit_of_measurement": "W"}},
        ]
        self.now: dict[str, str] = {}

    def list_states(self, domain: str | None = None) -> list[dict[str, Any]]:
        return self.states

    def get_history_many(self, ids: list[str], start: datetime, end: datetime | None = None) -> dict[str, list[tuple[datetime, str]]]:
        return {k: v for k, v in self.history.items() if k in ids}

    def get_state_or_none(self, entity_id: str) -> dict[str, Any] | None:
        if entity_id not in self.now:
            return None
        return {"entity_id": entity_id, "state": self.now[entity_id], "attributes": {"friendly_name": "Washing machine"}}


def test_device_costs_and_what_was_on_while_out() -> None:
    from app.household import away, device_costs

    now = NOW
    pump = [(now - timedelta(days=30, hours=1), "100"), (now - timedelta(days=15), "160"), (now - timedelta(hours=1), "220")]
    pc = [(now - timedelta(days=10), "90")]  # on at 90 W the whole time
    siris = SirisAI(settings=Settings(energy_flat_rate=0.3, energy_currency="$", timezone="UTC"))
    siris._home_assistant_client = FakeHA({"sensor.pool_pump_energy": pump, "sensor.office_pc_power": pc})  # type: ignore[attr-defined]
    siris.state.set(away.AWAY_LOG_KEY, [[(now - timedelta(days=2)).isoformat(), (now - timedelta(days=2) + timedelta(hours=5)).isoformat()]])
    out = device_costs.report(siris, 30, now)
    assert out["devices"][0] == {"entity_id": "sensor.pool_pump_energy", "name": "Pool pump", "kwh": 120.0, "kwh_month": 120.0, "cost_month": 36.0}
    assert out["while_away"][0]["name"] == "Office PC" and out["while_away"][0]["watts"] == 90 and out["while_away"][0]["hours"] == 5.0
    assert out["standby"][0]["watts"] == 90
    assert "Pool pump 120 kWh, about $36.00 a month" in out["text"] and "On while everyone was out: Office PC about 90 W for 5 h ($0.14)" in out["text"]


def test_the_away_log_records_when_the_house_was_empty() -> None:
    from app.household import away

    siris = SirisAI(settings=Settings())
    siris.state.delete(away.AWAY_LOG_KEY)
    away.log_change(siris, False, NOW)
    away.log_change(siris, False, NOW + timedelta(minutes=5))  # still out: no new period
    away.log_change(siris, True, NOW + timedelta(hours=2))
    assert away.away_periods(siris) == [(NOW, NOW + timedelta(hours=2))]


# -- 5. the network map -----------------------------------------------------------------


class FakeDocker:
    def published_ports(self) -> dict[str, list[int]]:
        return {"homeassistant": [8123], "plex": [32400], "sonarr": [8989]}


def test_what_breaks_if_the_nas_goes_down() -> None:
    from app.homelab import network_map

    siris = SirisAI(settings=Settings(ssh_hosts="nas=brad@192.168.0.127,pi=root@192.168.0.83", ssh_self_server="nas",
                                      home_assistant_base_url="http://192.168.0.127:8123", plex_base_url="http://192.168.0.127:32400",
                                      sirisos_url="http://192.168.0.83:8000", docker_base_url="tcp://192.168.0.127:2375"))
    siris._docker_client = FakeDocker()  # type: ignore[attr-defined]
    m = network_map.build(siris)
    services = {s["name"]: s for s in m["services"]}
    assert services["Home Assistant"]["container"] == "homeassistant" and services["SirisOS"]["server"] == "pi"
    nas = siris.run_tool("network_map", {"what": "nas"})
    assert nas["text"] == ("If nas goes down: SirisAI itself (and so every automation, reminder and alert); Home Assistant, Plex; "
                           "3 containers (homeassistant, plex, sonarr).")
    assert siris.run_tool("network_map", {"what": "Plex"})["text"] == "Plex runs in plex on nas."
    assert siris.run_tool("network_map", {"what": "pi"})["services"] == ["SirisOS"]
    assert "nas (192.168.0.127): SirisAI; Home Assistant, Plex; 3 containers" in siris.run_tool("network_map", {})["text"]
    with pytest.raises(ValueError, match="I don't know 'toaster'"):
        siris.run_tool("network_map", {"what": "toaster"})


# -- 10. the brief, adapted -------------------------------------------------------------


def test_the_brief_leads_with_what_changed() -> None:
    from app.skills.builtin import briefing_adapt

    siris = SirisAI(settings=Settings())
    for key in (briefing_adapt.PREFS_KEY, briefing_adapt.LAST_KEY):
        siris.state.delete(key)
    day1 = "Weather: 20°C, sunny. To-do (2): milk, bread. All systems normal. Active goals (1): Run 5k."
    assert briefing_adapt.adapt(siris, day1, date(2026, 11, 12)) == day1
    day2 = "Weather: 18°C, rain. To-do (2): milk, bread. 1 warning(s): nas disk 91% full. Active goals (1): Run 5k."
    assert briefing_adapt.adapt(siris, day2, date(2026, 11, 13)) == (
        "Weather: 18°C, rain. 1 warning(s): nas disk 91% full. Same as yesterday: to-do, goals.")
    assert briefing_adapt.adapt(siris, day2, date(2026, 11, 13)) == day2  # a second brief the same day isn't folded
    siris.run_tool("briefing_preferences", {"hide": ["goals"], "first": ["warnings"]})
    assert briefing_adapt.adapt(siris, day2, date(2026, 11, 20)) == (
        "1 warning(s): nas disk 91% full. Weather: 18°C, rain. To-do (2): milk, bread.")
    with pytest.raises(ValueError, match="isn't a part of the brief"):
        siris.run_tool("briefing_preferences", {"hide": ["horoscope"]})


# -- 11. reminders on a device's state ---------------------------------------------------


def test_a_reminder_when_the_washing_finishes() -> None:
    from app.reminders import conditions

    siris = SirisAI(settings=Settings())
    siris.state.delete(conditions.STATE_KEY)
    ha = FakeHA({})
    ha.now = {"sensor.washer_power": "350", "binary_sensor.dishwasher": "on"}
    siris._home_assistant_client = ha  # type: ignore[attr-defined]
    made = conditions.create(siris, "sensor.washer_power", "hang the washing out", below=5)
    assert made["message_text"] == "I'll remind you to hang the washing out when Washing machine drops below 5."
    conditions.create(siris, "binary_sensor.dishwasher", "unload the dishwasher", to_state="off")
    with pytest.raises(ValueError, match="either to_state"):
        conditions.create(siris, "sensor.washer_power", "x")
    assert conditions.check(siris) == []
    ha.now["sensor.washer_power"] = "2"
    fired = conditions.check(siris)
    assert [f["message"] for f in fired] == ["hang the washing out"]
    assert "Reminder: hang the washing out (Washing machine is 2)" in _feed(siris, "reminders")
    ha.now["binary_sensor.dishwasher"] = "off"
    assert [f["message"] for f in conditions.check(siris)] == ["unload the dishwasher"] and conditions.listed(siris) == []


# -- 12. undo history --------------------------------------------------------------------


def test_undo_more_than_the_last_thing() -> None:
    from app.core import undo

    siris = SirisAI(settings=Settings())
    siris.state.delete(undo.KEY)
    siris.state.set(undo.KEY, [
        {"at": (datetime.now(UTC) - timedelta(minutes=12)).isoformat(), "user": "default", "label": "turned on the lounge lights", "steps": []},
        {"at": (datetime.now(UTC) - timedelta(minutes=3)).isoformat(), "user": "default", "label": "set the heating to 21", "steps": []},
    ])
    listed = siris.run_tool("undo_history", {})
    assert listed["text"].startswith("I can undo: 1. set the heating to 21 (3 min ago); 2. turned on the lounge lights (12 min ago)")
    assert undo.find(siris, "the lights")["label"] == "turned on the lounge lights"
    assert undo.find(siris, "10 minutes ago")["label"] == "turned on the lounge lights"
    assert undo.find(siris, "2")["label"] == "turned on the lounge lights" and undo.find(siris, "9") is None
    done = undo.undo_last(siris, which="the lights")
    assert done["undone"] == "turned on the lounge lights"
    assert [e["label"] for e in undo.history(siris)] == ["set the heating to 21"]


# -- 14. answers ready ahead of time -----------------------------------------------------


def test_common_answers_are_fetched_before_the_usual_time(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core import prefetch
    from app.plugins.base import Tool, ToolPermission

    class Weather(Tool):
        name = "weather_current"
        description = "x"
        permission = ToolPermission.READ
        parameters = {"type": "object", "properties": {}, "required": []}
        calls = 0

        def run(self, **kwargs: Any) -> Any:
            Weather.calls += 1
            return {"temperature_c": 20 + Weather.calls}

    siris = SirisAI(settings=Settings(prefetch_fresh_seconds=600, timezone="UTC"))
    siris.tools._tools["weather_current"] = Weather()
    prefetch.clear()
    entries = [{"tool_name": "weather_current", "error": None, "created_at": (NOW - timedelta(days=d)).replace(hour=7, minute=5).replace(tzinfo=None).isoformat()}
               for d in (1, 2, 3, 4)]
    monkeypatch.setattr(siris.audit, "since", lambda since, limit=500: entries)
    assert prefetch.usual_hours(siris, NOW) == {"weather_current": {7}}
    assert prefetch.run(siris, NOW.replace(hour=6, minute=50)) == []  # not yet
    assert prefetch.run(siris, NOW.replace(hour=6, minute=56)) == ["weather_current"] and Weather.calls == 1
    assert prefetch.run(siris, NOW.replace(hour=6, minute=58)) == []  # once per hour
    assert siris.run_tool("weather_current", {}) == {"temperature_c": 21} and Weather.calls == 1  # the kept answer
    prefetch.clear()
    assert siris.run_tool("weather_current", {}) == {"temperature_c": 22}


# -- 13. the weekly review ----------------------------------------------------------------


class FakeBrain:
    def activity(self, days: int) -> list[dict[str, Any]]:
        return [{"line": "🧠 Learned [[SirisAI]]"}, {"line": "Decision: keep the NAS on ZFS"}]


def test_the_weekly_review_has_the_week_beyond_the_notes() -> None:
    from app.servers import timeline
    from app.upkeep import weekly_review

    siris = SirisAI(settings=Settings(timezone="UTC"))
    siris.state.delete(timeline.STATE_KEY)
    timeline.add(siris, "nas", "power", "rebooted and came back")
    siris.second_brain = FakeBrain()  # type: ignore[assignment]
    review = weekly_review.compose(siris, datetime.now(UTC))
    assert review["title"].endswith("-review") and "## Notes and decisions\n- 🧠 Learned [[SirisAI]]" in review["content"]
    assert "## Servers\n- nas: rebooted and came back" in review["body"]
    assert siris.run_tool("weekly_review", {})["sections"]["Servers"] == ["- nas: rebooted and came back"]


# -- 15. SirisAI's own health -------------------------------------------------------------


def test_the_health_page(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.llm import ollama
    from app.upkeep import dashboard

    ollama.FALLBACKS.append({"at": datetime.now(UTC).isoformat(), "from": "http://gpu:11434", "to": "http://mac:11434", "why": "ConnectError"})
    siris = SirisAI(settings=Settings())
    page = dashboard.build(siris)
    assert {"replies", "tools", "fallbacks", "jobs", "checks", "problems", "text"} <= set(page)
    assert any("fallback machine" in p for p in page["problems"])
    assert client.get("/siris/upkeep/dashboard").status_code == 200
    ollama.FALLBACKS.clear()
