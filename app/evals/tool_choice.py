"""Tool-choice evals: does SirisAI offer, and then pick, the right tool for
everyday requests? Cases live in evals/tool_choice.jsonl -- a prompt and the
tool(s) that would be a correct first call.

Two levels:
- **Selection** (no model, deterministic, runs in CI): is a correct tool among
  the ones the tool selector offers for that prompt? A tool that isn't
  offered can't be called, so this is the floor under everything else.
- **Choice** (the real model): offered those tools, does the model's first
  tool call land on a correct one? Run on demand (POST /siris/evals/run,
  `python -m app.evals`) and weekly by the scheduler; results are kept, and a
  drop of 10+ points from the previous run is pushed as a regression.

Cases learned from real corrections (app/learning/reflection.py) join the
checked-in ones in every run.
"""

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.core.siris import SirisAI

CASES_PATH = Path(__file__).resolve().parents[2] / "evals" / "tool_choice.jsonl"
HISTORY_KEY = "tool_choice_eval_history"
REGRESSION_POINTS = 10.0


def load_cases(path: Path = CASES_PATH) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def all_cases(siris: "SirisAI") -> list[dict[str, Any]]:
    """The checked-in cases plus the ones the nightly reflection learned
    from real corrections (app/learning/reflection.py)."""
    from app.learning.reflection import learned_cases

    cases = load_cases()
    known = {c["prompt"].strip().lower() for c in cases}
    return cases + [c for c in learned_cases(siris) if c["prompt"].strip().lower() not in known]


def applicable(siris: "SirisAI", cases: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Cases this configuration can serve at all -- a case whose tools aren't
    registered (e.g. email isn't set up here) is skipped, not failed."""
    usable = [c for c in cases if any(siris.tools.get(name) is not None for name in c["expect"])]
    return usable, len(cases) - len(usable)


def selection_report(siris: "SirisAI", cases: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    cases, skipped = applicable(siris, cases if cases is not None else all_cases(siris))
    failures = []
    for case in cases:
        offered = {s["function"]["name"] for s in siris.available_tool_specs(case["prompt"])}
        if not offered & set(case["expect"]):
            failures.append({"prompt": case["prompt"], "expect": case["expect"]})
    passed = len(cases) - len(failures)
    return {"cases": len(cases), "skipped": skipped, "passed": passed, "accuracy": round(100 * passed / max(len(cases), 1), 1), "failures": failures}


async def _choose(siris: "SirisAI", prompt: str) -> str | None:
    assert siris.llm is not None
    tools = siris.available_tool_specs(prompt)
    messages = [
        {"role": "system", "content": "You are SirisAI, a home assistant. Use a tool when one fits the request."},
        {"role": "user", "content": prompt},
    ]
    response = await siris.llm.generate(messages, tools=tools)
    return response.tool_calls[0].name if response.tool_calls else None


def choice_report(siris: "SirisAI", cases: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    cases, skipped = applicable(siris, cases if cases is not None else all_cases(siris))

    async def run_all() -> list[dict[str, Any]]:
        results = []
        for case in cases:
            try:
                chosen = await _choose(siris, case["prompt"])
            except Exception as exc:  # a backend error counts as a miss, with why
                chosen = f"error: {type(exc).__name__}"
            results.append({**case, "chosen": chosen, "ok": chosen in case["expect"]})
        return results

    results = asyncio.run(run_all())
    passed = sum(r["ok"] for r in results)
    return {
        "cases": len(results),
        "skipped": skipped,
        "passed": passed,
        "accuracy": round(100 * passed / max(len(results), 1), 1),
        "failures": [{"prompt": r["prompt"], "expect": r["expect"], "chosen": r["chosen"]} for r in results if not r["ok"]],
    }


def run_and_record(siris: "SirisAI", notify: bool = True) -> dict[str, Any]:
    """Both levels, stored in history; pushes a regression if accuracy fell."""
    selection = selection_report(siris)
    choice = choice_report(siris)
    history: list[dict[str, Any]] = siris.state.get(HISTORY_KEY) or []
    previous = history[-1] if history else None
    entry = {
        "at": datetime.now(UTC).isoformat(),
        "selection_accuracy": selection["accuracy"],
        "choice_accuracy": choice["accuracy"],
        "choice_failures": choice["failures"][:20],
        "selection_failures": selection["failures"][:20],
    }
    history = [*history, entry][-26:]
    siris.state.set(HISTORY_KEY, history)
    regressed = previous is not None and previous["choice_accuracy"] - choice["accuracy"] >= REGRESSION_POINTS
    if regressed and notify and siris.tools.get("notify") is not None:
        misses = "; ".join(f"'{f['prompt']}' -> {f['chosen']}" for f in choice["failures"][:3])
        siris.run_tool(
            "notify",
            {
                "title": "SirisAI: tool choice got worse",
                "message": f"Tool-choice accuracy fell from {previous['choice_accuracy']}% to {choice['accuracy']}%. e.g. {misses}",
            },
            trigger="tool-choice eval: automated schedule",
        )
    return {**entry, "regressed": regressed, "previous": previous}
