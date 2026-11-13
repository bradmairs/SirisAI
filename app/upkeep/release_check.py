"""A check right after a new version starts (6.38).

The nightly self-test (app/upkeep/self_test.py) finds a regression by the
next morning at best. Now, when SirisAI starts on a version it hasn't
checked yet, it waits SIRISAI_RELEASE_CHECK_DELAY_SECONDS (120) for
everything to connect, then:

- checks every integration (integration_health: Home Assistant, SirisOS,
  SSH, Ollama, ...), naming the ones that were fine before the update and
  aren't now,
- runs the self-test (temperature, a light, the day) when a model is set,

and raises one alert listing what broke ("After updating to 6.38: Home
Assistant isn't reachable"), with a push, or a quiet notice that all is
well. Each version is checked once.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.core.siris import SirisAI

logger = logging.getLogger(__name__)
STATE_KEY = "release_check"
FEED_KEY = "release_check"
_started = time.monotonic()


def due(siris: "SirisAI") -> bool:
    from app import __version__

    if siris.settings.release_check_delay_seconds < 0:
        return False
    done = siris.state.get(STATE_KEY) or {}
    return done.get("version") != __version__ and time.monotonic() - _started >= siris.settings.release_check_delay_seconds


def run(siris: "SirisAI", now: datetime | None = None) -> dict[str, Any] | None:
    from app import __version__
    from app.upkeep.alerts import raise_alert

    now = now or datetime.now(UTC)
    before = siris.state.get(STATE_KEY) or {}
    down: list[str] = []
    tool = siris.tools.get("integration_health")
    if tool is not None:
        try:
            down = list(tool.run().get("down") or [])
        except Exception as exc:  # noqa: BLE001
            down = [f"the integration check itself ({exc})"]
    test: dict[str, Any] | None = None
    if siris.settings.llm_provider != "none":
        try:
            from app.upkeep import self_test

            test = self_test.run(siris, now)
        except Exception as exc:  # noqa: BLE001
            test = {"failed": 1, "results": [], "error": str(exc)}
    was_down = set(before.get("down") or [])
    newly = [d for d in down if d not in was_down] if before.get("version") else down
    wrong = [r for r in (test or {}).get("results") or [] if not r.get("right")]
    problems = [f"{name} isn't reachable" + ("" if name in newly else " (it wasn't before either)") for name in down]
    problems += [f"the self-test got the {r['name']} wrong (said: {r['answer'][:120]})" for r in wrong]
    if test and test.get("error"):
        problems.append(f"the self-test didn't run ({test['error'][:120]})")
    record = {"version": __version__, "previous": before.get("version"), "at": now.isoformat(), "down": down,
              "self_test": {"passed": (test or {}).get("passed"), "failed": (test or {}).get("failed")}, "problems": problems}
    siris.state.set(STATE_KEY, record)
    if newly or wrong or (test and test.get("error")):
        raise_alert(siris, FEED_KEY, f"After updating to {__version__}: " + "; ".join(problems[:2]) + ("..." if len(problems) > 2 else ""),
                    "\n".join(f"- {p}" for p in problems) + (f"\nThe last version was {before['version']}." if before.get("version") else ""),
                    severity="attention", push=True)
    else:
        siris.attention.resolve_key(FEED_KEY, f"{__version__} checked out")
        raise_alert(siris, f"{FEED_KEY}:ok:{__version__}", f"Updated to {__version__}: everything checks out",
                    "Integrations reachable" + (f", self-test {test['passed']} of {test['passed'] + test['failed']} right" if test else "")
                    + ("" if not down else f" (still down from before: {', '.join(down)})") + ".", severity="info")
    return record
