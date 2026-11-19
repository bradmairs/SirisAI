"""Learning from "Not right" on its own (7.27).

A 👎 (app/learning/thumbs.py) or a correction in chat (app/learning/observe.py)
becomes a learned tool-choice case. When the same tool is the right answer to
two or more requests it wasn't picked for, SirisAI proposes a fix: add those
requests to the tool's description as examples ("Use for requests like ...").
That helps both the selector (which reads descriptions) and the model.

Each proposal is tried before it's offered: the cases must go from failing to
passing with the fix (on the tool selector, and on the real model when one is
set up), and the whole selection test must not get worse. It goes to the feed
to approve; approved fixes are kept in state and applied to every request.
Nothing changes without a tap. Cases that only say which tool was wrong
(`avoid`) don't say which was right, so they're tested but never turned into
a fix.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

from app.hub.attention import action, register_action

if TYPE_CHECKING:
    from app.core.siris import SirisAI

logger = logging.getLogger(__name__)

FIXES_KEY = "tool_fixes"
SOURCE = "tool_fixes"
MIN_CASES = 2
MAX_EXAMPLES = 6
_trial: dict[str, list[str]] = {}


def approved(siris: "SirisAI") -> dict[str, list[str]]:
    return dict(siris.state.get(FIXES_KEY) or {})


def apply(siris: "SirisAI", specs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The specs with approved (and, while trying one, trial) examples added to descriptions."""
    fixes = approved(siris)
    for tool, examples in _trial.items():
        fixes[tool] = [*fixes.get(tool, []), *examples]
    if not fixes:
        return specs
    out = []
    for spec in specs:
        examples = fixes.get(spec["function"]["name"])
        if examples:
            fn = dict(spec["function"])
            quoted = "; ".join(f'"{e}"' for e in examples[-MAX_EXAMPLES:])
            fn["description"] = f"{fn.get('description') or ''} Use for requests like: {quoted}.".strip()
            spec = {**spec, "function": fn}
        out.append(spec)
    return out


@contextlib.contextmanager
def trying(tool: str, examples: list[str]) -> Iterator[None]:
    _trial[tool] = examples
    try:
        yield
    finally:
        _trial.pop(tool, None)


def _groups(siris: "SirisAI") -> dict[str, list[str]]:
    from app.learning.reflection import learned_cases

    done = approved(siris)
    groups: dict[str, list[str]] = {}
    for case in learned_cases(siris):
        for tool in case.get("expect") or []:
            if siris.tools.get(tool) is None:
                continue
            prompt = case["prompt"].strip()
            if prompt.lower() not in {e.lower() for e in done.get(tool, [])}:
                groups.setdefault(tool, []).append(prompt)
            break
    return {t: p for t, p in groups.items() if len(p) >= MIN_CASES}


def _score(siris: "SirisAI", cases: list[dict[str, Any]], with_model: bool) -> tuple[int, int]:
    from app.evals import tool_choice

    selected = tool_choice.selection_report(siris, cases)["passed"]
    chosen = tool_choice.choice_report(siris, cases)["passed"] if with_model else 0
    return selected, chosen


def try_fix(siris: "SirisAI", tool: str, prompts: list[str]) -> dict[str, Any] | None:
    """Whether adding `prompts` as examples makes those requests work, without making others worse."""
    from app.evals import tool_choice

    cases = [{"prompt": p, "expect": [tool]} for p in prompts]
    with_model = siris.settings.llm_provider != "none" and siris.llm is not None
    before = _score(siris, cases, with_model)
    floor_before = tool_choice.selection_report(siris)["accuracy"]
    examples = prompts[-MAX_EXAMPLES:]
    with trying(tool, examples):
        after = _score(siris, cases, with_model)
        floor_after = tool_choice.selection_report(siris)["accuracy"]
    already = before[0] == len(cases) and (not with_model or before[1] == len(cases))
    better = after[0] >= before[0] and (not with_model or after[1] >= before[1]) and after != before
    if already or not better or floor_after < floor_before:
        return None
    return {
        "tool": tool,
        "examples": examples,
        "cases": len(cases),
        "offered": [before[0], after[0]],
        "picked": [before[1], after[1]] if with_model else None,
        "floor": [floor_before, floor_after],
    }


def propose(siris: "SirisAI") -> list[dict[str, Any]]:
    """After the nightly tool tests: a feed item for each fix that works."""
    raised = []
    for tool, prompts in _groups(siris).items():
        try:
            fix = try_fix(siris, tool, prompts)
        except Exception:
            logger.exception("Trying a fix for %s failed", tool)
            continue
        if fix is None:
            continue
        n = fix["cases"]
        result = f"With it, the right tool is offered for {fix['offered'][1]} of {n} (was {fix['offered'][0]})"
        if fix["picked"] is not None:
            result += f" and the model picks it for {fix['picked'][1]} of {n} (was {fix['picked'][0]})"
        siris.attention.raise_item(
            f"{SOURCE}:{tool}",
            "suggestion",
            f"Learn from {n} corrections: use {tool} for these",
            source=SOURCE,
            severity="info",
            body=f"You said these were wrong: {'; '.join(repr(p) for p in fix['examples'])}. {result}, and nothing else gets worse.",
            actions=[action("apply", "Learn this", "primary"), action("dismiss", "Not now")],
            payload=fix,
        )
        raised.append(fix)
    return raised


def _apply(siris: "SirisAI", item: dict[str, Any], payload: dict[str, Any], trigger: str) -> tuple[Any, str]:
    fix = payload
    fixes = approved(siris)
    fixes[fix["tool"]] = list(dict.fromkeys([*fixes.get(fix["tool"], []), *fix["examples"]]))[-MAX_EXAMPLES:]
    siris.state.set(FIXES_KEY, fixes)
    return fixes[fix["tool"]], f"Learned: {fix['tool']} for {len(fix['examples'])} kinds of request"


def forget(siris: "SirisAI", tool: str) -> bool:
    fixes = approved(siris)
    found = fixes.pop(tool, None) is not None
    siris.state.set(FIXES_KEY, fixes)
    return found


register_action(SOURCE, "apply", _apply)
