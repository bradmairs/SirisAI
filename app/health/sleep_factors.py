"""What goes with better or worse sleep (6.48): health_sleep_factors.

Over the last 6 weeks, for each evening it compares the next night's sleep
(hours) and the next morning's readiness after evenings with and without:
alcohol (any drinks logged), a lot of caffeine (200 mg or more that day), a
late workout (starting at 7pm or later), and screen time (when Health Auto
Export sends it, the day's screen time above your median). A factor needs
at least 4 evenings each way. The two clearest differences are reported
("After a drink you slept 40 minutes less and readiness was 9 lower, over
6 evenings"). These are patterns in your data, not proof of cause.
"""

from __future__ import annotations

import statistics
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.core.siris import SirisAI

DAYS = 42
MIN_EACH = 4


def _late(siris: "SirisAI", workouts: list[dict[str, Any]]) -> set[str]:
    from app.core import clock

    zone = clock.get_zone(siris.settings.timezone)
    out = set()
    for w in workouts:
        text = str(w.get("start") or "")
        try:
            when = datetime.strptime(text, "%Y-%m-%d %H:%M:%S %z").astimezone(zone)
        except ValueError:
            try:
                when = datetime.fromisoformat(text)
                when = when.astimezone(zone) if when.tzinfo else when
            except ValueError:
                continue
        if when.hour >= 19:
            out.add(when.date().isoformat())
    return out


def find(siris: "SirisAI", on: date | None = None) -> dict[str, Any]:
    from app.health import overview

    on = on or overview.today(siris)
    data = overview.days(siris, on, DAYS + 2)
    ready = {r["date"]: r["score"] for r in overview.recent_readiness(siris, on, DAYS + 1) if r.get("score") is not None}
    late = _late(siris, siris.health.workouts((on - timedelta(days=DAYS + 1)).isoformat()))
    screen_key = next((k for k in ("screen_time_minutes", "screen_time_h", "screen_time") if any(k in (m or {}) for m in data.values())), None)
    screens = [float(m[screen_key]) for m in data.values() if screen_key and (m or {}).get(screen_key) is not None]
    screen_median = statistics.median(screens) if screens else None

    tests = {
        "a drink": lambda d, m: (m.get("alcohol_drinks") or 0) >= 1 if "alcohol_drinks" in m else None,
        "200 mg of caffeine or more": lambda d, m: (m.get("caffeine_mg") or 0) >= 200 if "caffeine_mg" in m else None,
        "a late workout": lambda d, m: d in late,
        "more screen time than usual": lambda d, m: (float(m[screen_key]) > screen_median) if screen_key and m.get(screen_key) is not None and screen_median is not None else None,
    }
    results = []
    for name, test in tests.items():
        with_sleep, without_sleep, with_ready, without_ready = [], [], [], []
        for i in range(1, DAYS + 1):
            evening = on - timedelta(days=i)
            morning = (evening + timedelta(days=1)).isoformat()
            m = data.get(evening.isoformat()) or {}
            has = test(evening.isoformat(), m)
            if has is None:
                continue
            slept = (data.get(morning) or {}).get("sleep_hours")
            if slept is not None:
                (with_sleep if has else without_sleep).append(float(slept))
            if morning in ready:
                (with_ready if has else without_ready).append(float(ready[morning]))
        if len(with_sleep) < MIN_EACH or len(without_sleep) < MIN_EACH:
            continue
        sleep_diff = statistics.mean(with_sleep) - statistics.mean(without_sleep)
        ready_diff = (statistics.mean(with_ready) - statistics.mean(without_ready)) if len(with_ready) >= MIN_EACH and len(without_ready) >= MIN_EACH else None
        weight = abs(sleep_diff) / 0.25 + (abs(ready_diff) / 4 if ready_diff is not None else 0)
        results.append({"factor": name, "evenings": len(with_sleep), "others": len(without_sleep), "sleep_diff_h": round(sleep_diff, 2),
                        "readiness_diff": round(ready_diff) if ready_diff is not None else None, "weight": round(weight, 2)})
    results.sort(key=lambda r: -r["weight"])
    clear = [r for r in results if r["weight"] >= 1][:2]

    def phrase(r: dict[str, Any]) -> str:
        mins = round(abs(r["sleep_diff_h"]) * 60)
        text = f"After {r['factor']} you slept {mins} minutes {'less' if r['sleep_diff_h'] < 0 else 'more'}"
        if r["readiness_diff"]:
            text += f" and readiness was {abs(r['readiness_diff'])} {'lower' if r['readiness_diff'] < 0 else 'higher'}"
        return text + f", over {r['evenings']} evenings"

    if clear:
        text = ". ".join(phrase(r) for r in clear) + ". These are patterns in your data over the last 6 weeks, not proof of cause."
    elif results:
        text = "Nothing stands out: " + ", ".join(r["factor"] for r in results) + " made little difference to your sleep over the last 6 weeks."
    else:
        text = "Not enough logged yet: each factor needs at least 4 evenings with it and 4 without (log drinks and caffeine, or let workouts sync)."
    return {"factors": results, "clearest": clear, "text": text}
