"""Thin client for Hermes Agent -- a separate homelab agent, with its own
memories, skills, and terminal access, running on the same host as
SirisAI. SirisAI delegates Docker/system-control tasks to it rather than
reimplementing what it already knows how to do.

Speaks an OpenAI-style chat completions shape
(POST {base_url}/v1/chat/completions), the convention most self-hosted
agent/inference servers expose (llama.cpp server, vLLM, LiteLLM proxies,
...). If Hermes Agent's actual endpoint turns out to differ, this is the
one place to adjust it -- HermesRunTaskTool (app/plugins/builtin/hermes.py)
only ever talks to this client, never httpx directly.

check_status() (a hardening pass) hits GET /v1/models -- also part of
that same OpenAI-compatible convention, and the standard lightweight way
to answer "is this server up" without spending a completion on it.
"""

from typing import Any

import httpx


class HermesClient:
    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str | None = None,
        timeout: float = 120.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        # Generous default timeout -- Hermes may do its own multi-step
        # tool use/terminal work before responding, not a single quick
        # inference call.
        self._model = model
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else None
        self._client = httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout, headers=headers, transport=transport)

    def run_task(self, instruction: str) -> str:
        response = self._client.post(
            "/v1/chat/completions",
            json={"model": self._model, "messages": [{"role": "user", "content": instruction}]},
        )
        response.raise_for_status()
        choices = response.json().get("choices") or []
        if not choices:
            return ""
        return choices[0].get("message", {}).get("content") or ""

    def check_status(self) -> dict[str, Any]:
        """Confirms Hermes is actually reachable and lists whatever models
        it currently reports -- useful both as its own "is Hermes up"
        answer and, since a deployment could rename or swap the model
        SIRISAI_HERMES_MODEL points at, a way to notice that without
        waiting for a real hermes_run_task call to fail on it."""
        response = self._client.get("/v1/models")
        response.raise_for_status()
        models = [m.get("id") for m in response.json().get("data", []) if m.get("id")]
        return {"reachable": True, "configured_model": self._model, "available_models": models}

    def close(self) -> None:
        self._client.close()
