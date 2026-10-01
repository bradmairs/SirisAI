"""Tool-choice evals (app/evals/tool_choice.py) and semantic tool matching
(app/plugins/selector.py)."""

from typing import Any

from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.siris import SirisAI, get_siris
from app.evals import tool_choice
from app.evals.setup import fully_configured_settings
from app.llm.base import LLMResponse, ToolCall
from app.main import app
from app.plugins.selector import ToolSelector


def test_selection_also_holds_on_a_partly_configured_server_with_a_tighter_cap(client: TestClient) -> None:
    """Found live: on a server with only some integrations, 'who's at the
    front door?' and 'every night at 11...' weren't offered their tools."""
    settings = Settings(
        tool_selection_max=20, home_assistant_base_url="http://h", home_assistant_token="t", frigate_base_url="http://f",
        email_imap_host="i", email_username="u", email_password="p", seventeentrack_api_key="k",
    )
    report = tool_choice.selection_report(SirisAI(settings=settings))
    assert report["failures"] == [], report["failures"]


def test_every_eval_case_gets_its_tool_offered(client: TestClient) -> None:
    """The selection floor, in CI: with every integration configured (176
    tools) and the real 32-tool cap, a correct tool must be among those
    offered for every case. A miss means a prompt like it can't be served."""
    siris = SirisAI(settings=fully_configured_settings(tool_selection_max=32))
    assert len(siris.tools.list()) > 150
    report = tool_choice.selection_report(siris)
    assert report["failures"] == [], report["failures"]
    assert report["cases"] >= 70


# -- semantic matching with a stand-in embedding model that knows synonyms

_CONCEPTS = {
    "parcel": {"parcel", "package", "delivery", "doorstep", "courier", "box", "boxes", "shipment"},
    "sleep": {"sleep", "rest", "tired", "slept", "bed", "knackered"},
    "car": {"car", "vehicle", "ev", "charge", "battery", "drive", "juice"},
    "weather": {"weather", "rain", "forecast", "brolly", "umbrella"},
}


def _embed(texts: list[str]) -> list[list[float]]:
    out = []
    for text in texts:
        words = set(text.lower().replace("_", " ").replace(":", " ").replace("?", " ").split())
        out.append([float(len(words & concept)) for concept in _CONCEPTS.values()] + [0.01])
    return out


def _spec(name: str, description: str) -> dict[str, Any]:
    return {"type": "function", "function": {"name": name, "description": description, "parameters": {}}}


_SPECS = [_spec(f"filler_tool_{i}", f"Does unrelated thing number {i}.") for i in range(40)] + [
    _spec("parcel_list", "Deliveries being tracked and where each package is."),
    _spec("health_summary", "Sleep, steps and heart rate from the phone."),
    _spec("car_status", "The vehicle's battery, range and charging."),
    _spec("weather_forecast", "Forecast and chance of rain."),
]


def test_semantic_matching_finds_tools_that_share_no_words_with_the_prompt() -> None:
    prompt = "any boxes sitting on the doorstep"  # no word overlap with parcel_list or its hints
    keywords_only = {s["function"]["name"] for s in ToolSelector(max_tools=8).select(prompt, _SPECS)}
    with_meaning = {s["function"]["name"] for s in ToolSelector(max_tools=8, embed=_embed).select(prompt, _SPECS)}
    assert "parcel_list" not in keywords_only
    assert "parcel_list" in with_meaning


def test_tool_vectors_are_embedded_once_in_a_batch() -> None:
    calls: list[int] = []

    def counting(texts: list[str]) -> list[list[float]]:
        calls.append(len(texts))
        return _embed(texts)

    selector = ToolSelector(max_tools=8, embed=counting)
    selector.select("am I knackered", _SPECS)
    selector.select("do I need a brolly", _SPECS)
    assert calls == [len(_SPECS), 1, 1]  # tools once, then just each prompt


def test_embedding_failure_falls_back_to_keywords() -> None:
    def broken(texts: list[str]) -> list[list[float]]:
        raise ConnectionError("ollama down")

    names = {s["function"]["name"] for s in ToolSelector(max_tools=8, embed=broken).select("weather forecast", _SPECS)}
    assert "weather_forecast" in names


def test_siris_wires_the_embedding_client_into_selection() -> None:
    assert SirisAI(settings=Settings()).tool_selector.embed is None
    siris = SirisAI(settings=Settings(embedding_provider="ollama"))
    assert siris.tool_selector.embed is not None
    assert SirisAI(settings=Settings(embedding_provider="ollama", tool_selection_semantic=False)).tool_selector.embed is None


# -- the model-level eval


class _PicksFirstMatching:
    """Calls the expected tool for most cases, a wrong one for the rest."""

    def __init__(self, wrong_every: int) -> None:
        self.n = 0
        self.wrong_every = wrong_every

    async def generate(self, messages: list[dict[str, Any]], *, tools: Any = None, **kwargs: object) -> LLMResponse:
        self.n += 1
        prompt = messages[-1]["content"]
        case = next(c for c in tool_choice.load_cases() if c["prompt"] == prompt)
        name = "echo" if self.n % self.wrong_every == 0 else case["expect"][0]
        return LLMResponse(tool_calls=[ToolCall(name=name, arguments={})])

    async def aclose(self) -> None:
        pass


def test_choice_report_history_and_regression_push(client: TestClient) -> None:
    sent: list[str] = []

    class Pushes:
        def send(self, message: str, title: str | None = None, actions: Any = None, priority: str | None = None) -> None:
            sent.append(message)

        def close(self) -> None:
            pass

    siris = SirisAI(settings=fully_configured_settings(tool_selection_max=32), llm=_PicksFirstMatching(wrong_every=1000))
    siris.tools.get("notify")._client = Pushes()  # type: ignore[union-attr]
    siris.state.delete(tool_choice.HISTORY_KEY)
    first = tool_choice.run_and_record(siris)
    assert first["choice_accuracy"] == 100.0 and first["selection_accuracy"] == 100.0 and not first["regressed"]

    siris.llm = _PicksFirstMatching(wrong_every=3)  # type: ignore[assignment]
    second = tool_choice.run_and_record(siris)
    assert second["regressed"] is True and second["choice_accuracy"] < 70
    assert sent and sent[0].startswith("Tool-choice accuracy fell from 100.0% to")
    assert len(siris.state.get(tool_choice.HISTORY_KEY)) == 2


def test_evals_routes(client: TestClient) -> None:
    siris = SirisAI(settings=fully_configured_settings(tool_selection_max=32))
    app.dependency_overrides[get_siris] = lambda: siris
    try:
        selection = client.post("/siris/evals/run?selection_only=true").json()["selection"]
        assert selection["accuracy"] == 100.0
        assert client.post("/siris/evals/run").status_code == 501  # no LLM configured
        assert isinstance(client.get("/siris/evals/history").json(), list)
    finally:
        app.dependency_overrides.pop(get_siris, None)


def test_cases_for_unconfigured_integrations_are_skipped_not_failed(client: TestClient) -> None:
    report = tool_choice.selection_report(SirisAI(settings=Settings(tool_selection_max=32)))
    assert report["skipped"] > 30  # email, car, Home Assistant... aren't set up here
    assert report["cases"] + report["skipped"] == len(tool_choice.load_cases())
    assert report["failures"] == []

