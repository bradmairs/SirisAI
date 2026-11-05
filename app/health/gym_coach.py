"""SirisGym's coach (4.29): two things SirisAI works out from what it already has.

- **After a workout** (`session_advice`): plain suggestions from the set-by-set
  analysis in gym_effort. Heart rate that barely came down between sets, reps
  falling away at the same weight, a lift that barely raised heart rate. Heart
  rate usually comes a minute at a time, so advice is per exercise, never about
  a single set.
- **Each week** (`weekly`): training, sleep, recovery and protein read together,
  into at most two changes for the coming week, most useful first, each with the
  numbers behind it. It says so when it hasn't enough to go on, rather than guess.

Rules, not a model: the same data always gives the same advice, and every figure
in it is one you can check.
"""

from __future__ import annotations

from datetime import date, timedelta
from statistics import mean
from typing import TYPE_CHECKING, Any

from app.health import gym, overview, scores

if TYPE_CHECKING:
    from app.core.siris import SirisAI

LOW_RECOVERY_BPM = 15   # came down less than this between sets
SHORT_REST_S = 150      # and the rest was short enough to do something about
REP_DROP = 3
EASY_EFFORT = 4.0
SESSION_ADVICE = 3
WEEKLY_CHANGES = 2
GOOD_PROTEIN_G_PER_KG = 1.6


def session_advice(sets: list[dict[str, Any]], exercises: list[dict[str, Any]]) -> list[str]:
    """Up to three suggestions from one workout's analysis, most useful first."""
    out: list[str] = []
    working: dict[str, list[dict[str, Any]]] = {}
    for s in sets:
        if s.get("type") != "warmup":
            working.setdefault(str(s.get("exercise")), []).append(s)
    for name, rows in working.items():
        rec = [(r["recovery_bpm"], r["rest_s"]) for r in rows if r.get("recovery_bpm") is not None and r.get("rest_s")]
        if len(rec) >= 2:
            bpm, rest = mean(x[0] for x in rec), mean(x[1] for x in rec)
            if bpm < LOW_RECOVERY_BPM and rest < SHORT_REST_S:
                out.append(f"{name}: your heart rate only came down {round(bpm)} bpm in about {round(rest)} s between sets, so you were still "
                           f"recovering when you started the next one. Try 30 s more rest.")
    for name, rows in working.items():
        for a, b in zip(rows, rows[1:]):
            if a.get("weight") and a.get("weight") == b.get("weight") and (a.get("reps") or 0) - (b.get("reps") or 0) >= REP_DROP:
                out.append(f"{name}: reps fell from {a['reps']} to {b['reps']} at {a['weight']:g} kg. Rest longer, or take the weight down for the later sets.")
                break
    for e in exercises:
        if e.get("effort") is not None and e["effort"] <= EASY_EFFORT and (e.get("sets") or 0) >= 2 and not e.get("rpe"):
            out.append(f"{e['exercise']} barely raised your heart rate (effort {e['effort']:g}/10). If the reps felt easy too, there's room to add weight. "
                       f"Small lifts often read low, so go by feel.")
    return out[:SESSION_ADVICE]


def _avg(days: scores.Days, metric: str, on: date, n: int) -> tuple[float | None, int]:
    vals = [float(v) for i in range(n) if (v := (days.get((on - timedelta(days=i)).isoformat()) or {}).get(metric)) is not None]
    return (mean(vals), len(vals)) if vals else (None, 0)


def weekly(siris: "SirisAI", on: date | None = None) -> dict[str, Any]:
    """{changes: [{title, why, area}], looked_at: [...], enough: bool, text}. Sleep and recovery first:
    they limit everything else. Then protein, then the training itself."""
    on = on or overview.today(siris)
    days = overview.days(siris, on)
    summary = gym.latest(siris) or {}
    changes: list[dict[str, str]] = []
    looked: list[str] = []

    slept, nights = _avg(days, "sleep_hours", on, 7)
    if nights >= 3 and slept is not None:
        looked.append("sleep")
        need = scores.sleep_need(days, on)
        if slept < need - 0.5:
            changes.append({"area": "sleep", "title": "Get to bed about 30 minutes earlier",
                            "why": f"You averaged {slept:.1f} h a night over {nights} nights, against the {need:.1f} h you usually need. "
                                   f"Strength and recovery both suffer first."})

    kept = [r["score"] for r in overview.recent_readiness(siris, on, 7) if r.get("score") is not None]
    if len(kept) >= 3:
        looked.append("recovery")
        if mean(kept) < 50:
            changes.append({"area": "recovery", "title": "Take a lighter week in the gym",
                            "why": f"Readiness averaged {round(mean(kept))} over {len(kept)} mornings. Half the sets and 10% lighter lets it come back up."})

    protein, eaten_days = _avg(days, "protein_g", on, 7)
    weight = next((float(w) for i in range(30) if (w := (days.get((on - timedelta(days=i)).isoformat()) or {}).get("weight_kg")) is not None), None)
    if protein is not None and eaten_days >= 3 and weight:
        looked.append("protein")
        target = round(weight * GOOD_PROTEIN_G_PER_KG)
        if protein < target * 0.85:
            changes.append({"area": "protein", "title": f"Add about {round(target - protein)} g of protein a day",
                            "why": f"You averaged {round(protein)} g over {eaten_days} days logged. At {weight:.0f} kg, {target} g a day is a good floor for building strength."})

    if summary:
        looked.append("training")
        week = summary.get("week") or {}
        under = [m["muscle"] for m in summary.get("muscles") or [] if m.get("status") == "under"]
        over = [m["muscle"] for m in summary.get("muscles") or [] if m.get("status") == "over"]
        if (summary.get("deload") or {}).get("suggest") and not any(c["area"] == "recovery" for c in changes):
            why = "; ".join((summary.get("deload") or {}).get("why") or []) or "your main lifts have stalled"
            changes.append({"area": "training", "title": "Take a deload week", "why": f"{why[:1].upper()}{why[1:]}."})
        if not week.get("sessions") and (summary.get("streak_weeks") or 0) >= 1:
            changes.append({"area": "training", "title": "Get one workout in early this week",
                            "why": f"Nothing logged yet this week, and you're {summary['streak_weeks']} weeks in a row."})
        if over:
            changes.append({"area": "training", "title": f"Ease off {', '.join(over[:2])}", "why": "Over 20 sets this week, past where more usually helps."})
        if under and week.get("sessions"):
            changes.append({"area": "training", "title": f"Add a few sets for {', '.join(under[:2])}", "why": "Under 10 sets this week, the usual minimum to keep growing."})

    picked = changes[:WEEKLY_CHANGES]
    enough = len(looked) >= 2
    if picked:
        text = " ".join(f"{c['title']}: {c['why']}" for c in picked)
    elif enough:
        text = f"Nothing to change this week: {', '.join(looked)} all look fine."
    else:
        text = "Not enough to go on yet: this needs at least two of sleep, readiness, protein and SirisGym's training."
    return {"date": on.isoformat(), "changes": picked, "looked_at": looked, "enough": enough, "text": text}
