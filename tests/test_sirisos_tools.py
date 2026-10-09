"""SirisOS tools (app/plugins/builtin/sirisos.py) against a fake SirisOS."""

import json
from typing import Any

import httpx
import pytest

from app.core import taint
from app.core.config import Settings
from app.core.siris import SirisAI
from app.sirisos.client import SirisOSClient, SirisOSError
from app.skills.builtin.briefing_extras import briefing_lines

KEY = "service-key"


class FakeSirisOS:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.headers.get("authorization") != f"Bearer {KEY}":
            return httpx.Response(401, json={"detail": "Authentication required."})
        path = request.url.path
        if path == "/api/v1/hub/apps":
            return httpx.Response(200, json={"apps": [
                {"id": "apd-pm", "name": "APD PM", "status": {"state": "down", "detail": "Timed out"}},
                {"id": "archive", "name": "Engineering Archive", "status": {"state": "ok", "detail": ""}},
                {"id": "gvw", "name": "GVW Timesheets", "status": {"state": "unconfigured"}},
            ]})
        if path == "/api/v1/engineering/standards":
            return httpx.Response(200, json={"query": request.url.params["query"], "hits": [{
                "document": {"id": "d1", "title": "Plumbing and drainage - Sanitary", "authority": "Standards Australia", "reference": "AS/NZS 3500.2", "edition": "2021"},
                "page": 41, "snippet": "minimum grade of 1.65% for DN 100", "score": 80, "citation": "AS/NZS 3500.2:2021 p.41",
            }]})
        if path == "/api/v1/engineering/calculators":
            return httpx.Response(200, json={"calculators": [
                {"id": "fullPipe", "title": "Full pipe Manning", "category": "Stormwater & pipes", "note": "",
                 "fields": [{"key": "d", "label": "Internal diameter", "unit": "m", "initial": "0.45"}, {"key": "n", "label": "Manning n", "unit": "", "initial": "0.013"}]},
            ]})
        if path == "/api/v1/engineering/calculators/fullPipe/run":
            body = json.loads(request.content)
            return httpx.Response(200, json={"calculator": "fullPipe", "title": "Full pipe Manning", "inputs": body["inputs"],
                                             "results": [{"label": "Flow", "value": "0.198 m³/s"}]})
        if path == "/api/v1/engineering/calculators/nope/run":
            return httpx.Response(404, json={"detail": "Unknown calculator: nope"})
        if path == "/api/v1/projects":
            return httpx.Response(200, json={"projects": [
                {"id": "p1", "name": "Werribee Drainage", "description": "", "kind": "civil", "status": "active", "tags": ["stormwater"], "created_at": "x", "updated_at": "y"},
                {"id": "p2", "name": "Old job", "description": "", "kind": "civil", "status": "archived", "tags": [], "created_at": "x", "updated_at": "y"},
            ]})
        if path == "/api/v1/engineering/sirishydro/evidence":
            return httpx.Response(200, json={
                "question": request.url.params["question"], "sufficient_evidence": True,
                "evidence": [{"citation": "AS/NZS 3500.2:2021 p.41", "excerpt": "minimum grade of 1.65% for DN 100"}],
                "context_text": "...", "guidance": "Review the cited pages.", "synthesized_answer": "1.65% for DN 100 [1].",
            })
        return httpx.Response(404, json={"detail": "not found"})


@pytest.fixture
def setup(client: Any) -> tuple[SirisAI, FakeSirisOS]:
    fake = FakeSirisOS()
    siris = SirisAI(settings=Settings(sirisos_url="http://sirisos", sirisos_service_key=KEY))
    client = SirisOSClient("http://sirisos", KEY, transport=httpx.MockTransport(fake.handler))
    for tool in siris.tools.list():
        if isinstance(getattr(tool, "_client", None), SirisOSClient):
            tool._client = client  # type: ignore[attr-defined]
    return siris, fake


def test_tools_register_only_with_a_url(client: Any) -> None:
    names = {t.name for t in SirisAI(settings=Settings()).tools.list()}
    assert not names & {"engineering_calculate", "sirishydro_ask", "sirisos_apps_status"}


def test_apps_status(setup) -> None:
    siris, fake = setup
    result = siris.run_tool("sirisos_apps_status", {})
    assert result["needs_a_look"] == ["APD PM"]
    assert fake.requests[0].headers["x-siris-client"] == "SirisAI"


def test_standards_search_flattens_hits_and_taints_the_turn(setup) -> None:
    siris, _ = setup
    taint.begin_turn(None, "what's the minimum grade")
    result = siris.run_tool("engineering_standards_search", {"query": "minimum grade sewer", "limit": 50})
    assert result["hits"] == [{"standard": "AS/NZS 3500.2 Plumbing and drainage - Sanitary", "authority": "Standards Australia", "edition": "2021",
                               "page": 41, "citation": "AS/NZS 3500.2:2021 p.41", "excerpt": "minimum grade of 1.65% for DN 100"}]
    # Text from the PDFs is untrusted for this turn: a free-text writer
    # copying it would need confirmation.
    assert any("1.65%" in part for part in taint._context_parts.get())


def test_calculators_and_calculate(setup) -> None:
    siris, fake = setup
    listed = siris.run_tool("engineering_calculators", {})
    assert listed == [{"id": "fullPipe", "title": "Full pipe Manning", "category": "Stormwater & pipes", "inputs": {"d": "Internal diameter (m)", "n": "Manning n"}}]
    result = siris.run_tool("engineering_calculate", {"calculator": "fullPipe", "inputs": {"d": "0.45", "n": 0.013, "s": 0.005}})
    assert result["results"] == [{"label": "Flow", "value": "0.198 m³/s"}]
    assert json.loads(fake.requests[-1].content) == {"inputs": {"d": 0.45, "n": 0.013, "s": 0.005}}
    with pytest.raises(ValueError, match="must be a number"):
        siris.run_tool("engineering_calculate", {"calculator": "fullPipe", "inputs": {"d": "wide"}})
    with pytest.raises(SirisOSError, match="Unknown calculator"):
        siris.run_tool("engineering_calculate", {"calculator": "nope", "inputs": {}})


def test_projects_filter(setup) -> None:
    siris, _ = setup
    assert [p["name"] for p in siris.run_tool("engineering_projects", {"status": "active"})] == ["Werribee Drainage"]


def test_sirishydro(setup) -> None:
    siris, _ = setup
    result = siris.run_tool("sirishydro_ask", {"question": "Minimum grade for a DN 100 sanitary drain?"})
    assert result["answer"] == "1.65% for DN 100 [1]." and result["sufficient_evidence"] is True
    assert result["evidence"] == [{"n": 1, "citation": "AS/NZS 3500.2:2021 p.41", "excerpt": "minimum grade of 1.65% for DN 100"}]


def test_a_wrong_key_is_explained() -> None:
    fake = FakeSirisOS()
    client = SirisOSClient("http://sirisos", "wrong", transport=httpx.MockTransport(fake.handler))
    with pytest.raises(SirisOSError, match="refused the service key"):
        client.apps()


def test_briefing_and_integration_health_use_sirisos_app_states(setup, monkeypatch: pytest.MonkeyPatch) -> None:
    siris, _ = setup
    assert "Apps needing a look: APD PM is down (Timed out)." in briefing_lines(siris)
    monkeypatch.setattr("app.plugins.builtin.integration_health.check_integrations", lambda *a, **k: [{"name": "SirisOS", "ok": True, "detail": "reachable", "ms": 3}])
    health = siris.run_tool("integration_health", {})
    assert health["sirisos_apps"]["needs_a_look"] == ["APD PM"]


def test_sirisos_is_an_integration_target() -> None:
    from app.core.health import _integration_targets

    assert ("SirisOS", "http://os:8094/health") in _integration_targets(Settings(sirisos_url="http://os:8094/"))


def test_guest_mode_hides_project_names() -> None:
    from app.core.guest import is_personal_tool

    assert is_personal_tool("engineering_projects", "")
    assert not is_personal_tool("engineering_calculate", "")
