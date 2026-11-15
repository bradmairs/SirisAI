"""6.73: weekly reports in plain words, one card a week, pushes that fit
ntfy's body limit, and no momentary readings in Siris."""

import json
from datetime import UTC, datetime, timedelta

import httpx
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.siris import SirisAI
from app.db.models import AttentionItem, ToolExecution
from app.db.session import SessionLocal
from app.notify.client import MAX_BODY_BYTES, NotifyClient
from app.second_brain.jobs import momentary, parse_notes
from app.upkeep import plain, reliability


def test_errors_read_as_plain_words() -> None:
    lines = plain.summarise([
        ("notify", "Client error '413 Request Entity Too Large' for url 'https://ntfy.sh/'"),
        ("home_assistant_get_todo_items", "Server error '500 Internal Server Error' for url 'http://192.168.0.83:8123/api/x'"),
        ("home_assistant_get_state", "Client error '404 Not Found' for url 'http://192.168.0.83:8123/api/states/sensor.x'"),
        ("home_assistant_get_todo_items", "Server error '500 Internal Server Error'"),
        ("unifi_list_clients", "The read operation timed out"),
        ("presence_check", "[Errno 111] Connection refused"),
    ])
    assert lines[0] == "Home Assistant failed 3 times, mostly because it had a problem on its side. Check Home Assistant's own log (Settings, System, Logs)."
    assert "Push notifications failed once, because a message was too long to send." in lines[1]
    text = " ".join(lines)
    assert "http" not in text and "Errno" not in text and "mozilla" not in text


def test_the_reliability_report_is_one_card_a_week_in_plain_words(client: TestClient) -> None:
    with SessionLocal() as session:
        session.query(ToolExecution).delete()
        session.query(AttentionItem).delete()
        session.commit()
    siris = SirisAI(settings=Settings())
    siris.audit.record("unifi_list_clients", {}, error="The read operation timed out", trigger="presence")
    now = datetime.now(UTC) + timedelta(minutes=1)
    reliability.weekly(siris, now - timedelta(days=7))
    reliability.weekly(siris, now)
    reliability.weekly(siris, now)   # run twice the same week: still one card
    cards = [i for i in siris.attention.list() if i["source"] == "reliability"]
    assert len(cards) == 1
    assert "UniFi failed once, because it took too long to answer." in cards[0]["body"]
    assert "timed out" not in cards[0]["body"]


def test_a_push_body_always_fits_ntfy() -> None:
    sent: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request.content)
        return httpx.Response(200)

    client = NotifyClient("https://ntfy.example", "t", transport=httpx.MockTransport(handler))
    actions = [{"action": "http", "label": f"Do {n}", "url": "https://x.example/" + "a" * 300} for n in range(3)]
    client.send("“quoted” é " * 600, title="T" * 1000, actions=actions)
    assert len(sent[0]) <= MAX_BODY_BYTES
    body = json.loads(sent[0])
    assert body["message"].endswith("(more in SirisOS)") and len(body["title"]) <= 250


def test_momentary_readings_never_reach_siris() -> None:
    for title, content in [
        ("Lounge Lamp Status", "The lounge lamp is currently off."),
        ("Current Temperature Outside", "Current temperature outside is 17.6°C."),
        ("Day of the Week", "It's Sunday today."),
        ("TV Light 3", "TV Light 3 is off at the moment."),
    ]:
        assert momentary({"title": title, "content": content}), title
    for title, content in [
        ("Gym Training", "Brad trains upper body on Mondays and legs on Thursdays."),
        ("Evening Work Shutdown Protocol", "The office lights go off at 18:00 when the protocol runs."),
        ("Mealie", "Self-hosted recipe manager on the Home Server, port 9000."),
    ]:
        assert not momentary({"title": title, "content": content}), title
    reply = json.dumps({"notes": [{"title": "Lounge Lamp Status", "content": "The lounge lamp is off."},
                                  {"title": "Mealie", "content": "Recipe manager on the Home Server."}]})
    assert [n["title"] for n in parse_notes(reply)] == ["Mealie"]
