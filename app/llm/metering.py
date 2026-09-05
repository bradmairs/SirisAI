"""Records LLM token usage as metrics (milestone 47's cost/resource
dashboard), transparently to every caller.

Cortex.process() alone calls `.generate()` at up to three different
points (the initial call, the tool-result follow-up, and every step
Planner.run() takes), and confirm() adds a couple more -- recording
usage at each of those call sites individually would mean touching
Cortex and Planner repeatedly for one cross-cutting concern. Wrapping
the LLMClient instead is exactly what app/llm/base.py's interface
exists for (its own docstring: "the rest of the app should only ever
talk to this interface"): SirisAI.__post_init__ wraps whatever client
build_llm_client() returns before anything else ever sees it, so Cortex
and Planner keep calling `.generate()` on what they still believe is a
plain LLMClient, and every call's usage -- when the backend reports
any -- gets recorded automatically.

Performance hardening pass: recording used to be up to three separate
synchronous `record_sample()` calls (prompt/completion/total tokens),
each its own DB round trip, directly in `generate()` -- an `async def`
method on the response path of every chat turn (Cortex/Planner both
`await` this). Same "blocking DB call in an async method blocks the
one shared event loop" issue app/cortex/orchestrator.py's module
docstring already covers for `run_tool()`/`ContextEngine.build()`, just
missed here since this file predates that pass. Now one batched
`record_samples()` call via `asyncio.to_thread()`: one DB round trip,
off the event loop, instead of three on it.
"""

import asyncio
from typing import Any

from app.llm.base import LLMClient, LLMResponse
from app.metrics.service import MetricsStore

METRIC_PROMPT_TOKENS = "llm_tokens_prompt"
METRIC_COMPLETION_TOKENS = "llm_tokens_completion"
METRIC_TOTAL_TOKENS = "llm_tokens_total"


class MeteredLLMClient(LLMClient):
    def __init__(self, inner: LLMClient, metrics: MetricsStore) -> None:
        self._inner = inner
        self._metrics = metrics

    async def generate(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        **kwargs: object,
    ) -> LLMResponse:
        response = await self._inner.generate(messages, tools=tools, **kwargs)

        samples: dict[str, float] = {}
        if response.prompt_tokens is not None:
            samples[METRIC_PROMPT_TOKENS] = float(response.prompt_tokens)
        if response.completion_tokens is not None:
            samples[METRIC_COMPLETION_TOKENS] = float(response.completion_tokens)
        if response.prompt_tokens is not None and response.completion_tokens is not None:
            samples[METRIC_TOTAL_TOKENS] = float(response.prompt_tokens + response.completion_tokens)
        if samples:
            await asyncio.to_thread(self._metrics.record_samples, samples)

        return response

    async def aclose(self) -> None:
        await self._inner.aclose()
