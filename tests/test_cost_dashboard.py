"""Cost/resource dashboard (milestone 47): LLM token spend
(app/llm/metering.py) and homelab power draw (app/skills/builtin/
server_health.py, reusing the existing Home Assistant integration),
both surfaced via GET /siris/metrics/dashboard -- direct reuse of
MetricsStore's existing sample/baseline machinery (milestone 27), not a
new store.
"""

from types import SimpleNamespace
from typing import Any

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.siris import get_siris
from app.db.base import Base
from app.llm.base import LLMResponse
from app.llm.metering import MeteredLLMClient
from app.llm.ollama import OllamaClient
from app.main import app
from app.metrics.service import MetricsStore
from app.skills.builtin.server_health import ServerHealthSkill


def _isolated_metrics() -> MetricsStore:
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    return MetricsStore(sessionmaker(bind=engine))


# -- MetricsStore: total()/sample_count()/latest_value() (new for this milestone)


def test_total_sums_every_recorded_sample() -> None:
    metrics = _isolated_metrics()
    for value in (100.0, 250.0, 50.0):
        metrics.record_sample("llm_tokens_prompt", value)

    assert metrics.total("llm_tokens_prompt") == 400.0


def test_total_is_zero_for_a_metric_with_no_samples() -> None:
    assert _isolated_metrics().total("nonexistent") == 0.0


def test_sample_count_counts_samples_for_that_metric_only() -> None:
    metrics = _isolated_metrics()
    metrics.record_sample("llm_tokens_prompt", 10.0)
    metrics.record_sample("llm_tokens_prompt", 20.0)
    metrics.record_sample("power_draw_watts", 100.0)

    assert metrics.sample_count("llm_tokens_prompt") == 2
    assert metrics.sample_count("power_draw_watts") == 1
    assert metrics.sample_count("nonexistent") == 0


def test_latest_value_is_the_most_recently_recorded_sample() -> None:
    metrics = _isolated_metrics()
    metrics.record_sample("power_draw_watts", 120.0)
    metrics.record_sample("power_draw_watts", 135.5)

    assert metrics.latest_value("power_draw_watts") == 135.5


def test_latest_value_is_none_with_no_samples() -> None:
    assert _isolated_metrics().latest_value("nonexistent") is None


# -- OllamaClient: parsing token usage out of a real response shape


def test_ollama_client_parses_token_usage_from_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "message": {"role": "assistant", "content": "hi"},
                "prompt_eval_count": 42,
                "eval_count": 17,
            },
        )

    client = OllamaClient(base_url="http://localhost:11434", model="llama3", transport=httpx.MockTransport(handler))
    import asyncio

    response = asyncio.run(client.generate([{"role": "user", "content": "hello"}]))
    assert response.prompt_tokens == 42
    assert response.completion_tokens == 17


def test_ollama_client_leaves_token_usage_none_when_backend_omits_it() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"message": {"role": "assistant", "content": "hi"}})

    client = OllamaClient(base_url="http://localhost:11434", model="llama3", transport=httpx.MockTransport(handler))
    import asyncio

    response = asyncio.run(client.generate([{"role": "user", "content": "hello"}]))
    assert response.prompt_tokens is None
    assert response.completion_tokens is None


# -- MeteredLLMClient: recording usage as metrics, transparently


class _FixedResponseClient:
    def __init__(self, response: LLMResponse) -> None:
        self._response = response

    async def generate(self, messages: list[dict[str, Any]], *, tools: Any = None, **kwargs: object) -> LLMResponse:
        return self._response

    async def aclose(self) -> None:
        pass


def test_metered_client_records_prompt_completion_and_total_when_both_present() -> None:
    import asyncio

    metrics = _isolated_metrics()
    inner = _FixedResponseClient(LLMResponse(text="ok", prompt_tokens=30, completion_tokens=12))
    metered = MeteredLLMClient(inner, metrics)

    response = asyncio.run(metered.generate([{"role": "user", "content": "hi"}]))

    assert response.text == "ok"  # the wrapped response passes through unchanged
    assert metrics.total("llm_tokens_prompt") == 30.0
    assert metrics.total("llm_tokens_completion") == 12.0
    assert metrics.total("llm_tokens_total") == 42.0


def test_metered_client_records_nothing_when_backend_reports_no_usage() -> None:
    import asyncio

    metrics = _isolated_metrics()
    inner = _FixedResponseClient(LLMResponse(text="ok"))
    metered = MeteredLLMClient(inner, metrics)

    asyncio.run(metered.generate([{"role": "user", "content": "hi"}]))

    assert metrics.sample_count("llm_tokens_prompt") == 0
    assert metrics.sample_count("llm_tokens_completion") == 0
    assert metrics.sample_count("llm_tokens_total") == 0


def test_metered_client_skips_total_when_only_one_side_is_known() -> None:
    """An edge case no real backend hits today, but the total should never
    be half-computed from a missing side."""
    import asyncio

    metrics = _isolated_metrics()
    inner = _FixedResponseClient(LLMResponse(text="ok", prompt_tokens=30, completion_tokens=None))
    metered = MeteredLLMClient(inner, metrics)

    asyncio.run(metered.generate([{"role": "user", "content": "hi"}]))

    assert metrics.total("llm_tokens_prompt") == 30.0
    assert metrics.sample_count("llm_tokens_total") == 0


# -- record_samples() / MeteredLLMClient batching (a performance hardening
# pass): three separate record_sample() round trips per LLM call, directly
# on the response path of every chat turn, became one record_samples()
# call via asyncio.to_thread() -- one DB round trip, off the event loop.


def test_record_samples_writes_every_metric_given() -> None:
    metrics = _isolated_metrics()
    metrics.record_samples({"llm_tokens_prompt": 30.0, "llm_tokens_completion": 12.0, "llm_tokens_total": 42.0})

    assert metrics.total("llm_tokens_prompt") == 30.0
    assert metrics.total("llm_tokens_completion") == 12.0
    assert metrics.total("llm_tokens_total") == 42.0


def test_record_samples_with_an_empty_dict_writes_nothing() -> None:
    metrics = _isolated_metrics()
    metrics.record_samples({})
    assert metrics.sample_count("llm_tokens_prompt") == 0


def test_metered_client_calls_record_samples_once_not_record_sample_three_times() -> None:
    """Regression guard for the batching: this used to be three separate
    record_sample() calls -- each its own session/commit/round trip --
    directly on generate()'s response path. The real claim isn't about
    how many INSERT statements SQLAlchemy emits (three rows still means
    three INSERTs, whether batched into one transaction or not) -- it's
    that there's one session/commit instead of three, which is what
    actually saves round trips. Spying on the store's own methods proves
    that directly rather than trying to infer it from SQL statement
    counts."""
    import asyncio
    from unittest import mock

    metrics = _isolated_metrics()
    inner = _FixedResponseClient(LLMResponse(text="ok", prompt_tokens=30, completion_tokens=12))
    metered = MeteredLLMClient(inner, metrics)

    with mock.patch.object(metrics, "record_sample") as record_sample, mock.patch.object(
        metrics, "record_samples", wraps=metrics.record_samples
    ) as record_samples:
        asyncio.run(metered.generate([{"role": "user", "content": "hi"}]))

    record_sample.assert_not_called()
    record_samples.assert_called_once_with(
        {"llm_tokens_prompt": 30.0, "llm_tokens_completion": 12.0, "llm_tokens_total": 42.0}
    )
    assert metrics.total("llm_tokens_total") == 42.0


def test_metered_client_generate_does_not_block_a_concurrent_call() -> None:
    """Same property milestone 52 established for run_tool()/
    ContextEngine.build(): a slow DB write recording usage must not block
    a second, concurrent generate() call sharing the same process.

    Uses the real production SessionLocal/engine (app/db/session.py), not
    an isolated in-memory StaticPool engine -- a StaticPool shares one raw
    SQLite connection across threads, which isn't safe for two genuinely
    concurrent flushes at once (confirmed live: it raised a SQLAlchemy
    FlushError under this exact test). The real engine's connection pool
    plus WAL mode is what milestone 52 already established as safe for
    concurrent cross-thread access -- same reason its own concurrency
    tests use the real engine too."""
    import asyncio
    import time

    from app.db.session import SessionLocal

    class _SlowMetricsStore(MetricsStore):
        def record_samples(self, samples: dict[str, float]) -> None:
            time.sleep(0.3)
            super().record_samples(samples)

    metrics = _SlowMetricsStore(SessionLocal)
    inner = _FixedResponseClient(LLMResponse(text="ok", prompt_tokens=30, completion_tokens=12))
    metered = MeteredLLMClient(inner, metrics)

    async def _run_both() -> float:
        start = time.monotonic()
        await asyncio.gather(
            metered.generate([{"role": "user", "content": "hi"}]),
            metered.generate([{"role": "user", "content": "hi"}]),
        )
        return time.monotonic() - start

    elapsed = asyncio.run(_run_both())
    assert elapsed < 0.5  # well under the 0.6s two serialized 0.3s writes would take


# -- ServerHealthSkill: power draw recording (reuses Home Assistant, no new integration)


class _FakeToolsRegistry:
    def __init__(self, has_home_assistant: bool) -> None:
        self._has_home_assistant = has_home_assistant

    def get(self, name: str) -> object | None:
        return object() if name == "home_assistant_get_state" and self._has_home_assistant else None


class _FakeSirisForPowerDraw:
    def __init__(self, power_sensor_entity_id: str, has_home_assistant: bool, sensor_state: str) -> None:
        self.tools = _FakeToolsRegistry(has_home_assistant)
        self.metrics = _isolated_metrics()
        self.settings = SimpleNamespace(power_sensor_entity_id=power_sensor_entity_id)
        self._sensor_state = sensor_state

    def run_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        if name == "system_info":
            return {"cpu_percent": 5.0, "memory": {"percent_used": 5.0}, "disk": {}}
        if name == "home_assistant_get_state":
            assert arguments == {"entity_id": self.settings.power_sensor_entity_id}
            return {"entity_id": arguments["entity_id"], "state": self._sensor_state}
        raise AssertionError(f"unexpected tool call: {name}")


def test_power_draw_recorded_when_sensor_configured_and_home_assistant_available() -> None:
    siris = _FakeSirisForPowerDraw("sensor.pdu_watts", has_home_assistant=True, sensor_state="187.5")
    ServerHealthSkill().run(siris)
    assert siris.metrics.latest_value("power_draw_watts") == 187.5


def test_power_draw_not_recorded_when_sensor_not_configured() -> None:
    siris = _FakeSirisForPowerDraw("", has_home_assistant=True, sensor_state="187.5")
    ServerHealthSkill().run(siris)
    assert siris.metrics.sample_count("power_draw_watts") == 0


def test_power_draw_not_recorded_when_home_assistant_not_configured() -> None:
    siris = _FakeSirisForPowerDraw("sensor.pdu_watts", has_home_assistant=False, sensor_state="187.5")
    ServerHealthSkill().run(siris)
    assert siris.metrics.sample_count("power_draw_watts") == 0


def test_power_draw_read_failure_is_best_effort_and_does_not_fail_the_health_check() -> None:
    """A non-numeric sensor state (unavailable/unknown, Home Assistant's
    own way of saying a sensor has no current reading) shouldn't crash the
    health check -- same "an unreachable external service just means one
    fewer detail" reasoning as milestone 39's weather/calendar wrapping."""
    siris = _FakeSirisForPowerDraw("sensor.pdu_watts", has_home_assistant=True, sensor_state="unavailable")
    result = ServerHealthSkill().run(siris)
    assert result["healthy"] is True
    assert siris.metrics.sample_count("power_draw_watts") == 0


# -- GET /siris/metrics/dashboard


def test_dashboard_reflects_recorded_token_usage(client: TestClient) -> None:
    siris = get_siris()
    before = client.get("/siris/metrics/dashboard").json()

    siris.metrics.record_sample("llm_tokens_prompt", 100.0)
    siris.metrics.record_sample("llm_tokens_completion", 40.0)
    siris.metrics.record_sample("llm_tokens_total", 140.0)

    after = client.get("/siris/metrics/dashboard").json()

    assert after["llm_tokens"]["total_prompt_tokens"] == before["llm_tokens"]["total_prompt_tokens"] + 100.0
    assert after["llm_tokens"]["total_completion_tokens"] == before["llm_tokens"]["total_completion_tokens"] + 40.0
    assert after["llm_tokens"]["total_tokens"] == before["llm_tokens"]["total_tokens"] + 140.0
    assert after["llm_tokens"]["call_count"] == before["llm_tokens"]["call_count"] + 1


def test_dashboard_omits_power_draw_when_sensor_not_configured(client: TestClient) -> None:
    # The shared test SirisAI instance has no SIRISAI_POWER_SENSOR_ENTITY_ID
    # set (default empty), same as an unconfigured deployment.
    response = client.get("/siris/metrics/dashboard")
    assert response.json()["power_draw"] is None


def test_dashboard_includes_power_draw_when_sensor_configured(client: TestClient) -> None:
    from app.core.siris import SirisAI

    fake_siris = SirisAI(settings=get_siris().settings)
    fake_siris.metrics = _isolated_metrics()
    fake_siris.settings = SimpleNamespace(**{**vars(get_siris().settings), "power_sensor_entity_id": "sensor.pdu_watts"})
    fake_siris.metrics.record_sample("power_draw_watts", 150.0)
    fake_siris.metrics.record_sample("power_draw_watts", 160.0)

    app.dependency_overrides[get_siris] = lambda: fake_siris
    try:
        response = client.get("/siris/metrics/dashboard")
    finally:
        app.dependency_overrides.pop(get_siris, None)

    body = response.json()
    assert body["power_draw"]["latest_watts"] == 160.0
    assert body["power_draw"]["average_watts"] == 155.0
    assert body["power_draw"]["sample_count"] == 2
