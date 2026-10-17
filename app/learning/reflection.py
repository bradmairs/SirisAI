"""Nightly reflection: SirisAI looks back over the last day and learns.

1. **Outcomes.** It catches up on what happened after it acted: actions it
   took on its own that you undid or kept, chat turns you corrected, tool
   calls whose own check said they didn't work, and suggestions you've left
   alone for days (app/learning/observe.py).
2. **Evals from real mistakes.** A correction where a different tool ran
   afterwards becomes a tool-choice eval case ("that request should have
   gone to that tool"). These learned cases join evals/tool_choice.jsonl in
   every eval run, so the evals grow from real misses, not invented ones.
   `GET /siris/learning/eval-cases?format=jsonl` exports them to commit.
3. **Memory proposals.** With a model configured, it reads the day's
   corrections and failures and proposes up to three short, lasting facts
   ("'the big light' means light.living_room_main"). Each goes to the
   attention feed as "Remember this?" with Remember / No thanks; nothing is
   remembered without a yes. If you keep saying no, it proposes fewer.
4. **A short report**, kept in history (`GET /siris/learning/reflections`)
   and raised as an info item in the feed when there's something worth
   reading.

Runs once a night at SIRISAI_REFLECTION_HOUR_LOCAL (default 3), and on
demand at `POST /siris/learning/reflect`.
"""

import asyncio
import json
import logging
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from app.hub.attention import action, register_action
from app.learning import feedback, observe

if TYPE_CHECKING:
    from app.core.siris import SirisAI

logger = logging.getLogger(__name__)

LEARNED_CASES_KEY = "learned_eval_cases"
HISTORY_KEY = "reflection_history"
SOURCE = "learning"
MAX_LEARNED_CASES = 300
MAX_PROPOSALS = 3

_PROPOSAL_PROMPT = """You review a home assistant's mistakes from the last day and write down what it should remember about its user so they don't happen again.

Below are requests the user corrected and actions that failed. Write at most {n} short facts worth remembering for good: how the user names things, what they prefer, what they meant. Only facts clearly supported by the examples. No facts about one-off events. If nothing is worth keeping, return [].

Reply with only a JSON array of objects: [{{"fact": "...", "why": "..."}}]

{examples}"""


def learned_cases(siris: "SirisAI") -> list[dict[str, Any]]:
    return list(siris.state.get(LEARNED_CASES_KEY) or [])


def _add_cases(siris: "SirisAI", found: list[dict[str, Any]], today: str) -> list[dict[str, Any]]:
    cases = learned_cases(siris)
    known = {c["prompt"].strip().lower() for c in cases}
    added = []
    for c in found:
        right, request = c.get("right_tool"), (c.get("request") or "").strip()
        if not right or right == c["wrong_tool"] or not request or request.lower() in known:
            continue
        if siris.tools.get(right) is None:
            continue
        case = {"prompt": request[:500], "expect": [right], "learned": today, "instead_of": c["wrong_tool"]}
        cases.append(case)
        added.append(case)
        known.add(request.lower())
    if added:
        siris.state.set(LEARNED_CASES_KEY, cases[-MAX_LEARNED_CASES:])
    return added


def _parse_facts(text: str) -> list[dict[str, str]]:
    match = re.search(r"\[.*\]", text or "", re.DOTALL)
    if not match:
        return []
    try:
        found = json.loads(match.group(0))
    except json.JSONDecodeError:
        return []
    out = []
    for f in found if isinstance(found, list) else []:
        if isinstance(f, dict) and isinstance(f.get("fact"), str) and f["fact"].strip():
            out.append({"fact": f["fact"].strip()[:300], "why": str(f.get("why") or "")[:300]})
    return out


def _already_known(siris: "SirisAI", fact: str) -> bool:
    words = set(re.findall(r"\w+", fact.lower()))
    for memory in siris.memory.list_all():
        theirs = set(re.findall(r"\w+", memory["content"].lower()))
        if words and len(words & theirs) / len(words) >= 0.8:
            return True
    return False


def propose_memories(siris: "SirisAI", corrections: list[dict[str, Any]], failures: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if siris.llm is None or siris.settings.llm_provider == "none" or not (corrections or failures):
        return []
    n = 1 if feedback.muted(siris, f"attention:{SOURCE}") else MAX_PROPOSALS
    lines = [f"- Asked: \"{c['request'][:200]}\". It ran {c['wrong_tool']}. User said: \"{c['correction'][:200]}\"" + (f" Then {c['right_tool']} ran." if c.get("right_tool") else "") for c in corrections[:10]]
    lines += [f"- {f['tool']} didn't work: {str(f.get('why') or '')[:200]}" for f in failures[:10]]
    messages = [{"role": "user", "content": _PROPOSAL_PROMPT.format(n=n, examples="\n".join(lines))}]
    try:
        response = asyncio.run(siris.llm.generate(messages))
    except Exception:
        logger.exception("Reflection's memory proposal call failed")
        return []
    proposals = []
    for f in _parse_facts(response.text)[:n]:
        if _already_known(siris, f["fact"]):
            continue
        item = siris.attention.raise_item(
            f"learning:memory:{f['fact'].lower()[:200]}",
            "suggestion",
            f"Remember this? {f['fact']}",
            source=SOURCE,
            severity="info",
            body=(f["why"] + " " if f["why"] else "") + "I noticed this looking back over yesterday.",
            actions=[action("remember", "Remember", "primary"), action("dismiss", "No thanks")],
            payload={"fact": f["fact"]},
            reopen=False,
        )
        if item is not None:
            proposals.append(f)
    return proposals


def _remember(siris: "SirisAI", item: dict[str, Any], payload: dict[str, Any], trigger: str) -> tuple[Any, str]:
    result = siris.run_tool("remember", {"content": payload["fact"]}, trigger=trigger)
    return result, "Remembered"


register_action(SOURCE, "remember", _remember)


def _n(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


def reflect(siris: "SirisAI", now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    today = now.date().isoformat()
    undos = observe.undos(siris, now)
    failures = observe.unverified(siris, now)
    corrections = observe.corrections(siris, now)
    ignored = observe.ignored_items(siris, now)
    cases = _add_cases(siris, corrections, today)
    proposals = propose_memories(siris, corrections, failures)
    demoted = feedback.demoted_keys(siris)
    report = {
        "at": now.isoformat(),
        "kept": sum(1 for u in undos if u["signal"] == "kept"),
        "undone": [u["key"] for u in undos if u["signal"] == "undone"],
        "corrections": [{"request": c["request"][:200], "wrong_tool": c["wrong_tool"], "right_tool": c["right_tool"]} for c in corrections],
        "unverified": failures[:20],
        "ignored": [i["title"] for i in ignored],
        "learned_eval_cases": cases,
        "memory_proposals": [p["fact"] for p in proposals],
        "now_asking_first": demoted,
    }
    history = [*(siris.state.get(HISTORY_KEY) or []), report][-30:]
    siris.state.set(HISTORY_KEY, history)

    lines = []
    if report["undone"]:
        lines.append(f"You undid {_n(len(report['undone']), 'thing')} I did on my own: {', '.join(report['undone'][:3])}.")
    if report["now_asking_first"]:
        lines.append(f"I'll ask before doing these on my own again: {', '.join(demoted[:3])}.")
    if corrections:
        lines.append(f"You corrected me {_n(len(corrections), 'time')}" + (f"; {_n(len(cases), 'new eval case')} came of it." if cases else "."))
    if failures:
        lines.append(f"{_n(len(failures), 'action')} didn't check out afterwards: {', '.join(sorted({f['tool'] for f in failures}))[:200]}.")
    if lines:
        siris.attention.raise_item(
            f"learning:report:{today}", "notice", "What I learned last night", source=SOURCE, severity="info", body=" ".join(lines)
        )
    report["summary"] = " ".join(lines) or "Nothing new to learn from yesterday."
    return report
