"""Ask the model on this machine for a small JSON answer (SirisAI 4.6).

Documents, letters and conversation history are private, so the jobs that
read them (app/documents, app/conversation/summaries.py) only ever use a
model running here, never a cloud one. With none, they fall back to what
they can work out without one.
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.core.siris import SirisAI

logger = logging.getLogger(__name__)
_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def client(siris: "SirisAI") -> Any:
    """The general model when it's Ollama, else the fast one when that is."""
    if siris.settings.llm_provider == "ollama":
        return siris.llm
    if siris.settings.fast_llm_provider == "ollama":
        return getattr(siris.model_router, "_fast", None)
    return None


async def ask_json(siris: "SirisAI", instructions: str, text: str, llm: Any = None) -> dict[str, Any] | None:
    llm = llm or client(siris)
    if llm is None:
        return None
    try:
        response = await llm.generate(
            [{"role": "system", "content": instructions}, {"role": "user", "content": text}], format="json"
        )
    except Exception:  # noqa: BLE001 - every caller has a fallback
        logger.exception("Local model call failed")
        return None
    match = _JSON_RE.search(response.text or "")
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except ValueError:
        return None
    return data if isinstance(data, dict) else None
