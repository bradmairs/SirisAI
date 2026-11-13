"""Nightly self-test (6.0): ask SirisAI real questions and check the answers against the source.

The weekly tool-choice eval checks that the model picks the right tool; nothing checked that the
answer Brad hears is right. Each night (SIRISAI_SELF_TEST_HOUR_LOCAL, default 4) this asks the
whole pipeline -- quick commands, tool choice, the tool, the model's wording -- a few questions
it should get right, and checks each answer against the service itself:

- **weather**: "what's the temperature outside?" against the weather service (within 2 °C);
- **home**: whether a light is on or off, against Home Assistant (a different light each night);
- **date**: what day it is, against the clock.

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


CHECKS = (_weather_check, _home_check, _date_check)


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
        if right:
            siris.attention.resolve_key(key, "Answered right in the next self-test")
        else:
            raise_alert(
                siris, key, f"SirisAI answered a {check['name']} question wrong",
                f"Asked: {check['question']}\nIt said: {answer[:300]}\nBut {truth}.\n"
                "Check the tool it used and its wording (the conversation is " + conversation_id + ").",
                severity="attention",
            )
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
    return summary
