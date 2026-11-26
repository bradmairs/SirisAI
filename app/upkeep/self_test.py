"""Nightly self-test (6.0): ask SirisAI real questions and check the answers against the source.

The weekly tool-choice eval checks that the model picks the right tool; nothing checked that the
answer Brad hears is right. Each night (SIRISAI_SELF_TEST_HOUR_LOCAL, default 4) this asks the
whole pipeline -- quick commands, tool choice, the tool, the model's wording -- a few questions
it should get right, and checks each answer against the service itself:

- **weather**: "what's the temperature outside?" against the weather service (within 2 °C);
- **home**: whether a light is on or off, against Home Assistant (a different light each night);
- **date**: what day it is, against the clock;
- **plex** (7.17): what you've watched, against your own Tautulli history (and not someone else's).

A wrong answer goes into the attention feed with the question, the answer and what the source
says; a later right answer closes it. The questions run as a "self_test" turn, which may only
use READ tools (app/core/siris.py), so a test can never switch anything at 4 am, and they go in
their own conversation (self-test-<date>), not Brad's.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from app.core import clock

if TYPE_CHECKING:
    from app.core.siris import SirisAI

logger = logging.getLogger(__name__)

FEED_PREFIX = "self_test:"
RESULTS_KEY = "self_test_last"
WRONG_KEY = "self_test_wrong_nights"  # 4.30.0: {question name: nights wrong in a row}
WRONG_CARD_AFTER = 3
HISTORY_KEY = "self_test_history"
TEMPERATURE_TOLERANCE_C = 2.0
_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")
_ON_OFF = re.compile(r"\b(on|off)\b", re.I)
_DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def _weather_check(siris: "SirisAI", now: datetime) -> dict[str, Any] | None:
    client = getattr(siris, "_weather_client", None)
    if client is None:
        return None
    actual = client.current().get("temperature_c")
    if actual is None:
        return None

    def judge(answer: str) -> tuple[bool, str]:
        numbers = [float(n) for n in _NUMBER.findall(answer)]
        right = any(abs(n - float(actual)) <= TEMPERATURE_TOLERANCE_C for n in numbers)
        return right, f"the weather service says {actual} °C"

    return {"name": "weather", "question": "What's the temperature outside right now?", "judge": judge}


def _home_check(siris: "SirisAI", now: datetime) -> dict[str, Any] | None:
    client = getattr(siris, "_home_assistant_client", None)
    if client is None:
        return None
    lights = sorted(
        (s for s in client.list_states("light")
         if s.get("state") in ("on", "off") and (s.get("attributes") or {}).get("friendly_name")),
        key=lambda s: s["entity_id"],
    )
    if not lights:
        return None
    light = lights[now.toordinal() % len(lights)]   # a different one each night
    name = light["attributes"]["friendly_name"]

    def judge(answer: str) -> tuple[bool, str]:
        # Read the state again: someone may have switched it while the question ran.
        actual = (client.get_state_or_none(light["entity_id"]) or light).get("state")
        said = [w.lower() for w in _ON_OFF.findall(answer)]
        return bool(said) and said[0] == actual, f"Home Assistant says {light['entity_id']} is {actual}"

    return {"name": "home", "question": f"Is the {name} on or off right now?", "judge": judge}


def _date_check(siris: "SirisAI", now: datetime) -> dict[str, Any]:
    today = _DAYS[now.astimezone(clock.get_zone(siris.settings.timezone)).weekday()]

    def judge(answer: str) -> tuple[bool, str]:
        said = [d for d in _DAYS if re.search(rf"\b{d}\b", answer, re.I)]
        return said[:1] == [today], f"today is {today.title()}"

    return {"name": "date", "question": "What day of the week is it today?", "judge": judge}


def _plex_check(siris: "SirisAI", now: datetime) -> dict[str, Any] | None:
    """7.17: "what have I watched?" must name something you watched, not someone else's viewing."""
    tool = siris.tools.get("tautulli_recent_history")
    if tool is None:
        return None
    mine = tool.run(user="me", length=5)
    rows = mine["history"] if isinstance(mine, dict) else mine
    titles = [str(r.get("title") or "") for r in rows if r.get("title")]
    if not titles:
        return None
    shows = {t.split(" - ")[0].strip().lower() for t in titles} | {t.lower() for t in titles}
    everyone = tool.run(length=25)
    others = {
        str(r.get("title") or "").split(" - ")[0].strip().lower()
        for r in (everyone if isinstance(everyone, list) else [])
        if r.get("title")
    } - shows

    def judge(answer: str) -> tuple[bool, str]:
        said = answer.lower()
        right = any(s and s in said for s in shows)
        wrong = [o for o in others if o and len(o) > 3 and o in said]
        return right and not wrong, f"Tautulli says you watched {', '.join(titles[:3])}" + (f"; {wrong[0]} was someone else" if wrong else "")

    return {"name": "plex", "question": "What have I watched on Plex recently?", "judge": judge}


def _brain_questions(siris: "SirisAI") -> list[dict[str, Any]]:
    """Questions about Brad with a known answer, from the vault's search check (tools/eval_questions.json,
    entries with an "answer": words a right answer contains)."""
    import json
    from pathlib import Path

    brain = getattr(siris, "second_brain", None)
    if brain is None:
        return []
    path = Path(str(brain.path)) / "tools" / "eval_questions.json"
    try:
        cases = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [c for c in cases if isinstance(c, dict) and c.get("q") and c.get("answer")]


def _brain_check_for(slot: int):
    """4.27.0: three questions about Brad's own world each night, rotating, checked against the answer the
    vault records, so a wrong answer from his notes shows up the next morning (the search check only
    asks whether the right note is found)."""
    def build(siris: "SirisAI", now: datetime) -> dict[str, Any] | None:
        cases = _brain_questions(siris)
        if len(cases) <= slot:
            return None
        case = cases[(now.toordinal() * BRAIN_QUESTIONS_PER_NIGHT + slot) % len(cases)]
        expected = [str(a) for a in case["answer"]] if isinstance(case["answer"], list) else [str(case["answer"])]

        def judge(answer: str) -> tuple[bool, str]:
            return any(e.lower() in answer.lower() for e in expected), f"your notes say {' / '.join(expected)}"

        return {"name": f"brain{slot + 1}", "question": case["q"][:1].upper() + case["q"][1:], "judge": judge}

    build.__name__ = f"_brain{slot + 1}_check"
    return build


BRAIN_QUESTIONS_PER_NIGHT = 3
CHECKS = (_weather_check, _home_check, _date_check, _plex_check,
          *(_brain_check_for(i) for i in range(BRAIN_QUESTIONS_PER_NIGHT)))


async def _ask(siris: "SirisAI", conversation_id: str, question: str) -> str:
    from app.core.user_context import set_turn_role
    from app.cortex.orchestrator import Cortex

    set_turn_role("self_test")
    result = await Cortex().process(siris, conversation_id, question, use_planner=siris.settings.default_use_planner)
    if result.error:
        raise RuntimeError(result.error)
    return result.response or ""


async def _ask_all(siris: "SirisAI", conversation_id: str, questions: list[str]) -> list[str]:
    """One after another, in one task: the role stays "self_test" throughout and
    a failed turn becomes that question's answer."""
    answers = []
    for question in questions:
        try:
            answers.append(await _ask(siris, conversation_id, question))
        except Exception as exc:  # noqa: BLE001 - a failed turn is a wrong answer too
            answers.append(f"(no answer: {exc})")
    return answers


def run(siris: "SirisAI", now: datetime | None = None) -> dict[str, Any]:
    """Ask each question, judge each answer, and raise or close its feed item.
    Returns {"passed", "failed", "skipped", "results"}."""
    from app.upkeep.alerts import raise_alert

    now = now or datetime.now(UTC)
    conversation_id = f"self-test-{now.date().isoformat()}"
    results: list[dict[str, Any]] = []
    skipped: list[str] = []
    checks: list[dict[str, Any]] = []
    for build in CHECKS:
        name = build.__name__.strip("_").removesuffix("_check")
        try:
            check = build(siris, now)
        except Exception as exc:  # noqa: BLE001 - the source being down is the breakage watch's job
            logger.info("Self-test %s skipped: %s", name, exc)
            check = None
        if check is None:
            skipped.append(name)
        else:
            checks.append(check)
    from app.core.scheduler import run_on_main_loop

    answers = run_on_main_loop(_ask_all(siris, conversation_id, [c["question"] for c in checks])) if checks else []
    for check, answer in zip(checks, answers, strict=True):
        try:
            right, truth = check["judge"](answer)
        except Exception as exc:  # noqa: BLE001
            right, truth = False, f"the check itself failed ({exc})"
        results.append({"name": check["name"], "question": check["question"], "answer": answer[:300], "right": right, "truth": truth})
        key = FEED_PREFIX + check["name"]
        wrong_nights = dict(siris.state.get(WRONG_KEY) or {})
        if right:
            siris.attention.resolve_key(key, "Answered right in the next self-test")
            wrong_nights.pop(check["name"], None)
        else:
            # 4.30.0: one wrong answer is a line in Sunday's report, not a card on Home every morning (three of them
            # filled "Needs you" on 22-23 Nov). Wrong WRONG_CARD_AFTER nights running, it's a card; a real error
            # (the turn or a tool failing) is _escalate's, below, as before.
            from app.upkeep import week_review

            wrong_nights[check["name"]] = wrong_nights.get(check["name"], 0) + 1
            week_review.log(siris, "wrong", f"{check['name']} ({answer[:80]!r}, but {truth[:80]})", now)
            if wrong_nights[check["name"]] >= WRONG_CARD_AFTER:
                raise_alert(
                    siris, key, f"SirisAI has answered a {check['name']} question wrong {wrong_nights[check['name']]} nights running",
                    f"Asked: {check['question']}\nIt said: {answer[:300]}\nBut {truth}.\n"
                    "Check the tool it used and its wording (the conversation is " + conversation_id + ").",
                    severity="attention",
                )
        siris.state.set(WRONG_KEY, wrong_nights)
    _escalate(siris, results, conversation_id, now)
    summary = {
        "at": now.isoformat(),
        "passed": sum(r["right"] for r in results),
        "failed": sum(not r["right"] for r in results),
        "skipped": skipped,
        "results": results,
    }
    siris.state.set(RESULTS_KEY, summary)
    history = list(siris.state.get(HISTORY_KEY) or [])  # 6.58: for the health page
    history.append({"at": summary["at"], "passed": summary["passed"], "failed": summary["failed"]})
    siris.state.set(HISTORY_KEY, history[-30:])
    if results and not summary["failed"]:
        _clear_release_alert(siris)
    return summary


def _clear_release_alert(siris: "SirisAI") -> None:
    """4.27.1: an "After updating to ..." alert stayed until tapped, days after it was fixed (22-26 Nov 2026). Once a
    self-test gets everything right and every integration answers, it's closed."""
    from app.upkeep import release_check

    tool = siris.tools.get("integration_health")
    try:
        down = list(tool.run().get("down") or []) if tool is not None else []
    except Exception:  # noqa: BLE001
        return
    if not down:
        siris.attention.resolve_key(release_check.FEED_KEY, "Everything answers and the self-test is right again")


STREAK_KEY = "self_test_streak"
PUSH_AFTER, ISSUE_AFTER = 2, 3


def _errors(siris: "SirisAI", conversation_id: str, result: dict[str, Any]) -> list[str]:
    """Actual errors behind a wrong answer: the turn failing, the check failing, or a tool erroring in that conversation."""
    found = []
    if result["answer"].startswith("(no answer"):
        found.append(result["answer"])
    if result["truth"].startswith("the check itself failed"):
        found.append(result["truth"])
    try:
        found += [f"{e['tool_name']}: {e['error']}" for e in siris.audit.list_recent(limit=200)
                  if e.get("conversation_id") == conversation_id and e.get("error")]
    except Exception:  # noqa: BLE001
        pass
    return list(dict.fromkeys(found))[:4]


def _escalate(siris: "SirisAI", results: list[dict[str, Any]], conversation_id: str, now: datetime) -> None:
    """4.29.0: the Plex question failed four nights in a row (22-25 Nov 2026) with only a feed card to show for it.
    The same question failing with a real error two nights running is pushed to the phone with the error; a third
    night it's proposed as a GitHub issue (filed straight away with SIRISAI_GITHUB_ISSUES_MODE=auto)."""
    from app.upkeep.alerts import raise_alert

    streak: dict[str, int] = dict(siris.state.get(STREAK_KEY) or {})
    for r in results:
        errors = [] if r["right"] else _errors(siris, conversation_id, r)
        if not errors:
            if streak.pop(r["name"], None) and r["right"]:
                siris.attention.resolve_key(f"{FEED_PREFIX}{r['name']}:error", "Answered right again")
            continue
        streak[r["name"]] = streak.get(r["name"], 0) + 1
        nights = streak[r["name"]]
        if nights < PUSH_AFTER:
            continue
        raise_alert(
            siris, f"{FEED_PREFIX}{r['name']}:error",
            f"The {r['name']} self-test has failed {nights} nights running",
            f"Asked: {r['question']}\nError: {'; '.join(errors)[:600]}\n(conversation {conversation_id})",
            severity="urgent" if nights >= ISSUE_AFTER else "attention",
            push=True, issue=nights >= ISSUE_AFTER,
        )
    siris.state.set(STREAK_KEY, streak)
