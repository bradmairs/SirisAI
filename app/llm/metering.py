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
"""

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

        if response.prompt_tokens is not None:
            self._metrics.record_sample(METRIC_PROMPT_TOKENS, float(response.prompt_tokens))
        if response.completion_tokens is not None:
            self._metrics.record_sample(METRIC_COMPLETION_TOKENS, float(response.completion_tokens))
        if response.prompt_tokens is not None and response.completion_tokens is not None:
            self._metrics.record_sample(METRIC_TOTAL_TOKENS, float(response.prompt_tokens + response.completion_tokens))

        return response

    async def aclose(self) -> None:
        await self._inner.aclose()
