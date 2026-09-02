"""Model router: picks which configured `LLMClient` handles a given chat
turn. A thin selection layer on top of the existing `LLMClient` interface
(app/llm/base.py), not a new kind of client.

Two roles today: "general" -- the default model, still configured via
`SIRISAI_LLM_PROVIDER`/`SIRISAI_OLLAMA_*` exactly as before this milestone
-- and an optional "fast" one (`SIRISAI_FAST_LLM_*`) for short/simple
prompts. `ModelRouter` doesn't own the general client itself (it's passed
into `pick()` each call) so it always reflects whatever `SirisAI.llm`
currently is.

The pattern generalizes to more roles -- a coding model, a cloud model, a
"keep private data local" policy that never routes to the cloud one --
without a new mechanism: another settings block plus a case in `pick()`.
Not built out further here for lack of a second real backend to route
between yet.
"""

from app.llm.base import LLMClient


class ModelRouter:
    def __init__(self, fast: LLMClient | None, policy: str, fast_max_prompt_chars: int) -> None:
        self._fast = fast
        self._policy = policy
        self._fast_max_prompt_chars = fast_max_prompt_chars

    def pick(self, general: LLMClient, prompt: str, role: str | None = None) -> tuple[str, LLMClient]:
        """Return (role_used, client). An explicit `role` wins if that
        role is actually configured; an unavailable explicit role (e.g.
        "fast" with none configured) falls back to "general" rather than
        erroring. With no explicit role, the routing policy decides."""
        if role == "fast" and self._fast is not None:
            return "fast", self._fast
        if role == "general":
            return "general", general

        if self._policy == "prompt_length" and self._fast is not None and len(prompt) <= self._fast_max_prompt_chars:
            return "fast", self._fast

        return "general", general

    def available_roles(self) -> list[str]:
        return ["general", "fast"] if self._fast is not None else ["general"]

    async def aclose(self) -> None:
        """Release the "fast" client's resources, if configured. The
        general client is owned by SirisAI.llm and closed there."""
        if self._fast is not None:
            await self._fast.aclose()
