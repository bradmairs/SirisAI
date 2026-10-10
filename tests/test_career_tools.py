"""career_cpd_status and career_chartership_status, against a fake SirisOS
Career API (SirisOS ADR 111)."""

from typing import Any

import httpx
import pytest

from app.core import guest, taint
from app.core.config import Settings
from app.core.siris import SirisAI
from app.plugins.selector import ToolSelector
from app.sirisos.client import SirisOSClient

KEY = "service-key"

CAREER = {
    "document": {
        "evidence": [{"id": "e1", "title": "Pump station risk workshop", "elements": ["6"]}],
        "cpd": {"records": [
            {"id": "r1", "date": "2026-09-05", "title": "Stormwater design to AR&R 2019", "provider": "IPWEA", "hours": 7},
            {"id": "r2", "date": "2026-06-12", "title": "Ozwater 2026", "provider": "AWA", "hours": 14},
        ]},
    },
    "overview": {
        "cpd": {
            "window": {"from": "2023-10-10", "to": "2026-10-10", "years": 3}, "total": 56.5, "required": 150, "short": 93.5, "met": False,
            "minimums": [
                {"category": "area", "label": "Area of practice", "hours": 35, "minimum": 50, "short": 15},
                {"category": "risk", "label": "Risk management", "hours": 3.5, "minimum": 10, "short": 6.5},
                {"category": "business", "label": "Business & management", "hours": 17, "minimum": 15, "short": 0},
            ],
            "by_year": {"2025": 9.5, "2026": 21}, "expiring_90_days": 4, "guessed": 0, "records": 2,
            "last_import": {"at": "2026-10-09T08:00:00+00:00"}, "latest_activity": "2026-09-05",
        },
        "pathways": [
            {"id": "cpeng", "name": "Chartered Professional Engineer (CPEng)", "done": 1, "total": 8, "next": {"id": "evidence", "title": "Collect evidence for every element", "status": "todo"}},
            {"id": "vic-rpe", "name": "Registered Professional Engineer, Victoria (civil)", "done": 4, "total": 4, "next": None},
        ],
        "competencies": {"total": 16, "evidenced": 15, "counts": {}, "gaps": [{"id": "1", "unit": "Personal commitment", "title": "Deal with ethical issues"}]},
        "goals": [{"id": "g1", "title": "Chartered by June 2027", "target_date": "2027-06-30", "next_step": "Book the interview", "status": "active"}],
        "next_steps": [{"title": "Collect evidence for every element", "detail": "Chartered Professional Engineer (CPEng)", "kind": "pathway"}],
    },
}


@pytest.fixture
def siris(client: Any) -> SirisAI:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers.get("authorization") != f"Bearer {KEY}":
            return httpx.Response(401, json={"detail": "Authentication required."})
        if request.url.path == "/api/v1/career" and request.method == "GET":
            return httpx.Response(200, json=handler.body)
        return httpx.Response(404, json={"detail": "not found"})

    handler.body = CAREER  # type: ignore[attr-defined]
    siris = SirisAI(settings=Settings(sirisos_url="http://sirisos", sirisos_service_key=KEY))
    fake = SirisOSClient("http://sirisos", KEY, transport=httpx.MockTransport(handler))
    for tool in siris.tools.list():
        if tool.name.startswith("career_"):
            tool._client = fake  # type: ignore[attr-defined]
    siris._career_handler = handler  # type: ignore[attr-defined]
    return siris


def test_cpd_status_summarises_the_engineers_australia_record(siris) -> None:
    result = siris.run_tool("career_cpd_status", {})
    assert result["imported"] is True and result["window"] == "2023-10-10 to 2026-10-10"
    assert (result["total_hours"], result["required_hours"], result["hours_short"]) == (56.5, 150, 93.5)
    assert {m["category"]: m["short"] for m in result["minimums"]} == {"Area of practice": 15, "Risk management": 6.5, "Business & management": 0}
    assert result["hours_leaving_window_in_90_days"] == 4
    assert result["recent_activities"][0] == {"date": "2026-09-05", "title": "Stormwater design to AR&R 2019", "hours": 7, "provider": "IPWEA"}


def test_cpd_status_says_how_to_import_when_empty(siris) -> None:
    siris._career_handler.body = {"document": {"cpd": {"records": []}}, "overview": {"cpd": {"records": 0}}}
    result = siris.run_tool("career_cpd_status", {})
    assert result["imported"] is False and "Record my CPD" in result["how"]


def test_chartership_status_gives_next_steps_gaps_and_goals(siris) -> None:
    result = siris.run_tool("career_chartership_status", {})
    assert result["next_steps"] == ["Collect evidence for every element (Chartered Professional Engineer (CPEng))"]
    assert result["pathways"][0] == {"name": "Chartered Professional Engineer (CPEng)", "steps_done": 1, "steps": 8, "next_step": "Collect evidence for every element"}
    assert result["pathways"][1]["next_step"] is None
    assert result["competencies"] == {"elements_with_evidence": 15, "elements": 16, "still_need_evidence": ["1. Deal with ethical issues"]}
    assert result["goals"] == [{"goal": "Chartered by June 2027", "by": "2027-06-30", "next_step": "Book the interview"}]


def test_career_tools_are_read_only_personal_and_offered_for_career_questions(siris) -> None:
    tools = {t.name: t for t in siris.tools.list()}
    assert tools["career_cpd_status"].permission.name == "READ" and tools["career_chartership_status"].permission.name == "READ"
    assert {"career_cpd_status", "career_chartership_status"} <= guest._PERSONAL_TOOLS
    assert "career_cpd_status" in taint.EXTERNAL_CONTENT_TOOLS
    specs = siris.tools.specs()
    for question in ("how's my CPD going?", "what's my next chartership step", "how close am I to CPEng"):
        names = {s["function"]["name"] for s in ToolSelector(20).select(question, specs)}
        assert {"career_cpd_status", "career_chartership_status"} <= names, question


def test_not_registered_without_sirisos(client: Any) -> None:
    names = {t.name for t in SirisAI(settings=Settings()).tools.list()}
    assert not names & {"career_cpd_status", "career_chartership_status"}
