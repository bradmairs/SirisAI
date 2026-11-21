"""Memory tidy-up (7.49): a monthly look at what SirisAI remembers.

Memories were never reviewed: they piled up, went stale ("Brad's car is in
for service") and could contradict each other. On the 2nd of each month
this raises up to 8 feed cards:

- **out of date**: worded for a moment ("today", "this week", "currently",
  "next Tuesday", a date that's passed) and over 30 days old, or never
  recalled in 180 days -- Keep or Forget;
- **the same thing twice**: duplicates (MemoryStore.find_duplicate_groups)
  -- Keep the newest, which forgets the others;
- **contradicting**: two memories the local model says can't both be true
  (only when a model is set up) -- Keep the newer, or Keep both.

A memory you keep isn't asked about again for a year.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from app.hub.attention import action, register_action

if TYPE_CHECKING:
    from app.core.siris import SirisAI

logger = logging.getLogger(__name__)
SOURCE = "memory_tidy"
KEPT_KEY = "memory_tidy:kept"
MAX_CARDS = 8
_MOMENT = re.compile(
    r"\b(today|tonight|tomorrow|yesterday|this (?:week|weekend|month|morning|afternoon|evening)|next (?:week|month|monday|tuesday|"
    r"wednesday|thursday|friday|saturday|sunday)|currently|at the moment|right now|for now|is in for|is away|is sick|is staying)\b",
    re.I,
)
_DATE = re.compile(r"\b(20\d\d)-(\d\d)-(\d\d)\b")


def _naive(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=None)


def stale(memories: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    now = now.replace(tzinfo=None)
    out = []
    for m in memories:
        age = now - _naive(m["created_at"])
        last = _naive(m["last_recalled_at"]) if m.get("last_recalled_at") else None
        dates = [datetime(int(y), int(mo), int(d)) for y, mo, d in _DATE.findall(m["content"]) if 1 <= int(mo) <= 12 and 1 <= int(d) <= 31]
        if (_MOMENT.search(m["content"]) and age > timedelta(days=30)) or (dates and max(dates) < now - timedelta(days=1)):
            out.append({**m, "why": "it sounds like it was only true for a while"})
        elif age > timedelta(days=180) and (last is None or now - last > timedelta(days=180)):
            out.append({**m, "why": "it hasn't come up in six months"})
    return out


async def _conflicts(siris: "SirisAI", memories: list[dict[str, Any]]) -> list[tuple[int, int, str]]:
    if siris.llm is None or len(memories) < 2:
        return []
    listing = "\n".join(f"{m['id']}: {m['content']}" for m in memories[:80])
    ask = [
        {"role": "system", "content": "You find contradictions in lists of facts. Reply only with JSON."},
        {"role": "user", "content": (
            "Which pairs of these facts can't both be true now? Reply with a JSON list like "
            '[{"a": 3, "b": 9, "why": "different cars"}], or [] if none.\n\n' + listing
        )},
    ]
    try:
        reply = await siris.llm.generate(ask)
        found = json.loads((reply.text or "[]").strip().strip("`").removeprefix("json").strip())
    except Exception:
        logger.info("Memory contradiction check skipped", exc_info=True)
        return []
    ids = {m["id"] for m in memories}
    return [(int(p["a"]), int(p["b"]), str(p.get("why") or "")) for p in found if isinstance(p, dict) and p.get("a") in ids and p.get("b") in ids][:4]


def review(siris: "SirisAI", now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    kept = {int(k): v for k, v in (siris.state.get(KEPT_KEY) or {}).items()}
    fresh = lambda m: m["id"] not in kept or _naive(kept[m["id"]]) < now.replace(tzinfo=None) - timedelta(days=365)  # noqa: E731
    memories = [m for m in siris.memory.list_all() if fresh(m)]
    by_id = {m["id"]: m for m in memories}
    cards = 0
    raised: dict[str, int] = {"stale": 0, "duplicates": 0, "conflicts": 0}

    def card(key: str, title: str, body: str, actions: list[dict[str, Any]], payload: dict[str, Any], kind: str) -> None:
        nonlocal cards
        if cards >= MAX_CARDS:
            return
        if siris.attention.raise_item(f"{SOURCE}:{key}", "suggestion", title[:200], source=SOURCE, severity="info",
                                      body=body, actions=actions, payload=payload, reopen=False):
            cards += 1
            raised[kind] += 1

    try:
        groups = siris.memory.find_duplicate_groups()
    except Exception:
        groups = []
    for g in groups:
        items = sorted((by_id[i] for i in g.get("ids", []) if i in by_id), key=lambda m: m["created_at"])
        if len(items) < 2:
            continue
        newest = items[-1]
        card(f"dup:{newest['id']}", f"Remembered twice: {newest['content'][:80]}",
             "\n".join(f"• {m['content']}" for m in items),
             [action("keep_newest", "Keep the newest", "primary"), action("dismiss", "Leave them")],
             {"keep": newest["id"], "forget": [m["id"] for m in items[:-1]]}, "duplicates")
    for m in stale(memories, now):
        card(f"stale:{m['id']}", f"Still true? {m['content'][:120]}", f"I'm asking because {m['why']}.",
             [action("keep", "Still true"), action("forget", "Forget it", "primary")], {"id": m["id"]}, "stale")
    for a, b, why in asyncio.run(_conflicts(siris, memories)) if cards < MAX_CARDS else []:
        older, newer = sorted((by_id[a], by_id[b]), key=lambda m: m["created_at"])
        card(f"conflict:{older['id']}:{newer['id']}", f"These disagree: {newer['content'][:80]}",
             f"Newer: {newer['content']}\nOlder: {older['content']}" + (f"\n({why})" if why else ""),
             [action("keep_newer", "Keep the newer", "primary"), action("keep", "Both are right")],
             {"keep": newer["id"], "forget": [older["id"]], "ids": [older["id"], newer["id"]]}, "conflicts")
    return {"memories": len(memories), **raised}


def _remember_kept(siris: "SirisAI", ids: list[int]) -> None:
    kept = dict(siris.state.get(KEPT_KEY) or {})
    for i in ids:
        kept[str(i)] = datetime.now(UTC).replace(tzinfo=None).isoformat()
    siris.state.set(KEPT_KEY, kept)


def _forget(siris: "SirisAI", item: dict[str, Any], payload: dict[str, Any], trigger: str) -> tuple[Any, str]:
    ok = siris.memory.delete(int(payload["id"]))
    return {"forgotten": ok}, "Forgotten"


def _keep(siris: "SirisAI", item: dict[str, Any], payload: dict[str, Any], trigger: str) -> tuple[Any, str]:
    _remember_kept(siris, [int(i) for i in (payload.get("ids") or [payload.get("id")]) if i is not None])
    return None, "Kept"


def _keep_one(siris: "SirisAI", item: dict[str, Any], payload: dict[str, Any], trigger: str) -> tuple[Any, str]:
    gone = [i for i in payload.get("forget", []) if siris.memory.delete(int(i))]
    _remember_kept(siris, [int(payload["keep"])])
    return {"forgotten": gone}, f"Kept one, forgot {len(gone)}"


register_action(SOURCE, "forget", _forget)
register_action(SOURCE, "keep", _keep)
register_action(SOURCE, "keep_newest", _keep_one)
register_action(SOURCE, "keep_newer", _keep_one)
