"""Keep a long conversation within what the model can read (7.37).

Every turn used to replay the whole conversation, every tool result
included. A small local model has a few thousand tokens to work with, and
Ollama drops the oldest part when it runs out -- the personality and the
instructions at the top -- so long chats got slower and drifted (the voice,
"never show code"). Now:

- the last SIRISAI_HISTORY_KEEP_MESSAGES (12) messages are replayed word
  for word, never splitting a tool call from its result;
- tool results before those are cut to SIRISAI_HISTORY_TOOL_CHARS (1500)
  -- a 200-item list doesn't need replaying in full;
- if what's kept is still over SIRISAI_HISTORY_MAX_CHARS (16000), older
  messages move into the summary until it fits;
- everything older is one short "Earlier in this conversation" note: a
  model-written summary when one is ready (refreshed in the background
  after a turn, so it never slows a reply), else a plain digest of what
  was asked and answered.

The system messages (personality, context) are added after this, so they
are always there.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.core.siris import SirisAI

logger = logging.getLogger(__name__)

SUMMARY_KEY = "history_summary:{}"
DIGEST_CHARS = 1500
_REFRESH_AFTER = 6   # new dropped messages before the model summary is rewritten


def _size(m: dict[str, Any]) -> int:
    return len(str(m.get("content") or "")) + len(json.dumps(m.get("tool_calls") or []))


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else f"{text[:limit]} ... ({len(text) - limit} more characters left out)"


def _split(messages: list[dict[str, Any]], keep: int) -> int:
    """Where the kept tail starts: `keep` from the end, moved so it never starts on a tool result."""
    start = max(0, len(messages) - keep)
    while start < len(messages) and messages[start].get("role") == "tool":
        start += 1   # a tool result needs its call; start after the pair
    return start


def digest(messages: list[dict[str, Any]]) -> str:
    """A plain note of what was asked and answered, newest kept when it's long."""
    lines = []
    for m in messages:
        role, text = m.get("role"), str(m.get("content") or "").strip().replace("\n", " ")
        if role == "user" and text:
            lines.append(f"You were asked: {text[:140]}")
        elif role == "assistant" and m.get("tool_calls"):
            names = ", ".join(c.get("function", {}).get("name", "") for c in m["tool_calls"])
            lines.append(f"(looked up with {names})")
        elif role == "assistant" and text:
            lines.append(f"You answered: {text[:140]}")
    out: list[str] = []
    used = 0
    for line in reversed(lines):
        if used + len(line) > DIGEST_CHARS:
            break
        out.append(line)
        used += len(line) + 1
    return "\n".join(reversed(out))


def compact(siris: "SirisAI", conversation_id: str, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    s = siris.settings
    keep = max(2, int(getattr(s, "history_keep_messages", 12) or 12))
    tool_chars = int(getattr(s, "history_tool_chars", 1500) or 1500)
    max_chars = int(getattr(s, "history_max_chars", 16000) or 16000)
    start = _split(messages, keep)
    tail = [dict(m) for m in messages[start:]]
    # older tool results in the tail (all but the last exchange) are cut too
    last_user = max((i for i, m in enumerate(tail) if m.get("role") == "user"), default=0)
    for i, m in enumerate(tail):
        if m.get("role") == "tool" and i < last_user:
            m["content"] = _clip(str(m.get("content") or ""), tool_chars)
    while sum(_size(m) for m in tail) > max_chars and len(tail) > 2:
        drop = 1
        while drop < len(tail) and tail[drop].get("role") == "tool":
            drop += 1
        start += drop
        tail = tail[drop:]
    if start == 0:
        return tail
    older = messages[:start]
    cached = siris.state.get(SUMMARY_KEY.format(conversation_id)) or {}
    if cached.get("text") and cached.get("covers", 0) >= start - _REFRESH_AFTER:
        note = cached["text"]
    else:
        note = digest(older)
    if not note:
        return tail
    return [{"role": "system", "content": f"Earlier in this conversation (summarised):\n{note}"}, *tail]


async def refresh_summary(siris: "SirisAI", conversation_id: str, messages: list[dict[str, Any]]) -> str | None:
    """Rewrite the model summary of what's no longer replayed, when enough is new. After a turn."""
    s = siris.settings
    start = _split(messages, max(2, int(getattr(s, "history_keep_messages", 12) or 12)))
    if start < _REFRESH_AFTER or siris.llm is None:
        return None
    key = SUMMARY_KEY.format(conversation_id)
    cached = siris.state.get(key) or {}
    if cached.get("covers", 0) > start - _REFRESH_AFTER:
        return None
    text = digest(messages[:start])
    ask = [
        {"role": "system", "content": "Summarise conversations briefly and exactly."},
        {"role": "user", "content": (
            "In at most 6 short lines, note what was asked, what was found or done, and anything still open, "
            "so the conversation can carry on. Facts only.\n\n" + (cached.get("text", "") + "\n" if cached.get("text") else "") + text
        )},
    ]
    try:
        reply = await siris.llm.generate(ask)
    except Exception:
        logger.info("History summary skipped", exc_info=True)
        return None
    summary = (reply.text or "").strip()
    if summary:
        siris.state.set(key, {"covers": start, "text": summary[:2000]})
    return summary or None
