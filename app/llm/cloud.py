"""When to ask a hosted model instead of the local one (7.27). Off by default.

SIRISAI_CLOUD_LLM_WHEN:
- "never" (default): everything stays on your own hardware, as before.
- "fallback": only when the local model fails (down, timed out, errored).
- "hard": also for questions that need more than a small model -- long
  ones, and ones asking to plan, write, draft, explain why, compare,
  summarise or brainstorm.

Never sent, whatever the setting:
- questions about health, sleep, training, money, passwords or the like
  (they stay local), and anything while guest mode is on, or from a child
  or guest voice, or SirisAI's own self-test;
- health and training data from tools: those answers are always written
  by the local model (orchestrator._private_answer, SIRISAI_HEALTH_PRIVATE).

A monthly cap (SIRISAI_CLOUD_LLM_MONTHLY_CALLS, default 200) stops it after
that many questions; the count is on the health page. What is sent: the
question, the conversation so far and the context SirisAI adds (memories,
the time, your home's state) -- the same as the local model gets.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.core.siris import SirisAI
    from app.llm.base import LLMClient

_HARD = re.compile(
    r"\b(plan|planning|write|draft|rewrite|explain why|why does|why do|why is|compare|comparison|pros and cons|summar(?:y|ise|ize)|"
    r"brainstorm|itinerary|essay|analy[sz]e|strategy|step[- ]by[- ]step|recommend|advice|should i)\b",
    re.I,
)
_PRIVATE = re.compile(
    r"\b(health|sleep|slept|hrv|heart|weight|bmi|gym|workout|training|steps|calories|diet|medication|medicine|doctor|symptom|"
    r"bank|money|salary|budget|spend|spending|bill|bills|debt|tax|password|passcode|pin|ssn|tfn|passport)\b",
    re.I,
)
HARD_LENGTH = 300


def configured(siris: "SirisAI") -> bool:
    s = siris.settings
    return s.cloud_llm_when in ("fallback", "hard") and bool(s.cloud_llm_base_url and s.cloud_llm_model)


def _month_key(now: datetime | None = None) -> str:
    return f"cloud_llm_calls:{(now or datetime.now(UTC)).strftime('%Y-%m')}"


def used_this_month(siris: "SirisAI") -> int:
    return int(siris.state.get(_month_key()) or 0)


def allowed(siris: "SirisAI", prompt: str) -> bool:
    """May this question go to the hosted model at all (privacy, role, budget)?"""
    from app.core import guest
    from app.core.user_context import get_turn_role

    if not configured(siris) or siris.cloud_llm is None:
        return False
    if get_turn_role() in ("child", "guest", "self_test"):
        return False
    try:
        if guest.is_on(siris):
            return False
    except Exception:
        return False
    if _PRIVATE.search(prompt or ""):
        return False
    return used_this_month(siris) < siris.settings.cloud_llm_monthly_calls


def is_hard(prompt: str) -> bool:
    return len(prompt or "") >= HARD_LENGTH or bool(_HARD.search(prompt or ""))


def choose(siris: "SirisAI", prompt: str) -> "LLMClient | None":
    """The hosted client for a hard question, when allowed; else None (stay local)."""
    if siris.settings.cloud_llm_when == "hard" and is_hard(prompt) and allowed(siris, prompt):
        return siris.cloud_llm
    return None


def fallback(siris: "SirisAI", prompt: str) -> "LLMClient | None":
    """The hosted client when the local model failed, when allowed."""
    return siris.cloud_llm if allowed(siris, prompt) else None


def count(siris: "SirisAI") -> int:
    used = used_this_month(siris) + 1
    siris.state.set(_month_key(), used)
    try:
        siris.metrics.record_sample("cloud_llm_calls", 1)
    except Exception:  # noqa: BLE001
        pass
    return used
