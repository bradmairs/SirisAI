"""Effort in the gym, set by set (4.28).

SirisGym (SirisOS 6.96+) records the time each set is ticked done, and sends
the session here when it's saved: each set's exercise, weight, reps, RPE and
time. The Apple Watch workout started with it (a Shortcut can start it as the
workout starts) comes in through Health Auto Export's Workouts automation
with its heart rate over time (turn on heart-rate data there).

The two are linked by time: the Watch workout that overlaps the session most
(from 20 minutes before it starts to 5 minutes after it ends). Each set is
then aligned with the heart rate around it:

- **the set**: from its estimated start (about 4 s a rep, at least 20 s, or
  its seconds for a timed set, never before the previous set ended) to 20 s
  after it was ticked, when heart rate peaks;
- **the rest after it**: to the next set's start, and how far heart rate came
  back down during it (recovery: the peak less the lowest point, in bpm).

Per set: average and peak heart rate and the peak as a share of your maximum
heart rate (the one strain uses). Per exercise: those across its working
sets, an **effort** score 0-10 (from the peaks against your maximum, blended
with your RPE where you gave one), the best estimated 1RM that session
(**strength**), and recovery. Per session: average and peak heart rate,
energy, time in each zone, work against rest, the hardest exercise and the
slowest recovery, in words. Health Auto Export's workout heart rate is
usually a point a minute, so a set's figures are the minutes it overlaps:
good for comparing exercises and sessions, rough for a single set.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from statistics import mean
from typing import TYPE_CHECKING, Any

from app.core.user_context import get_current_user_id

if TYPE_CHECKING:
    from app.core.siris import SirisAI

KEEP_SERIES = 150
KEEP_SESSIONS = 300


def _aware(text: Any) -> datetime | None:
    s = str(text or "").strip().replace("T", " ").replace("Z", "+0000")
    for fmt in ("%Y-%m-%d %H:%M:%S %z", "%Y-%m-%d %H:%M:%S%z", "%Y-%m-%d %H:%M:%S.%f%z", "%Y-%m-%d %H:%M %z"):
        try:
            return datetime.strptime(s.replace("+00:00", "+0000"), fmt)
        except ValueError:
            continue
    try:
        t = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
        return t if t.tzinfo else t.replace(tzinfo=UTC)
    except (TypeError, ValueError):
        return None


def _key(kind: str) -> str:
    return f"{kind}:{get_current_user_id()}"


def keep_series(siris: "SirisAI", series: list[dict[str, Any]]) -> int:
    if not series:
        return 0
    kept = dict(siris.state.get(_key("workout_series")) or {})
    for s in series:
        kept[s["start"]] = s
    for k in sorted(kept, key=lambda k: str(_aware(k) or k))[:-KEEP_SERIES]:
        kept.pop(k)
    siris.state.set(_key("workout_series"), kept)
    return len(series)


def keep_session(siris: "SirisAI", session: dict[str, Any]) -> None:
    kept = dict(siris.state.get(_key("gym_sessions")) or {})
    kept[str(session["id"])] = session
    for k in sorted(kept, key=lambda k: str(kept[k].get("started_at")))[:-KEEP_SESSIONS]:
        kept.pop(k)
    siris.state.set(_key("gym_sessions"), kept)


def _e1rm(weight: Any, reps: Any) -> float | None:
    try:
        w, r = float(weight or 0), int(reps or 0)
    except (TypeError, ValueError):
        return None
    if not w or not r or r > 12:
        return None
    return round(w if r == 1 else w * (1 + r / 30), 1)


def _band(share: float) -> int:
    return 0 if share < 0.5 else min(5, 1 + int((share - 0.5) * 10))


def link(siris: "SirisAI", session: dict[str, Any]) -> dict[str, Any] | None:
    a, b = _aware(session.get("started_at")), _aware(session.get("ended_at"))
    if a is None:
        return None
    b = b or a + timedelta(minutes=60)
    best, overlap = None, 0.0
    for s in (siris.state.get(_key("workout_series")) or {}).values():
        sa = _aware(s.get("start"))
        se = _aware(s.get("end")) or (sa + timedelta(minutes=60) if sa else None)
        if sa is None or se is None:
            continue
        lo, hi = max(sa, a - timedelta(minutes=20)), min(se, b + timedelta(minutes=5))
        o = (hi - lo).total_seconds()
        if o > overlap:
            best, overlap = s, o
    return best


def analyse(siris: "SirisAI", session_id: str) -> dict[str, Any]:
    from app.health import strain

    session = (siris.state.get(_key("gym_sessions")) or {}).get(session_id)
    if not session:
        return {"linked": False, "why": "SirisAI hasn't had this workout from SirisGym yet (it's sent when the workout is saved)."}
    watch = link(siris, session)
    if not watch:
        return {"linked": False, "why": "No Apple Watch workout overlaps it yet. Start one with the workout (a Shortcut can do it), and make sure Health "
                                        "Auto Export's Workouts automation sends heart-rate data; it links when that arrives."}
    hr = [(t, avg, top) for p in watch["hr"] if (t := _aware(p[0])) is not None for avg, top in [(float(p[1]), float(p[2]))]]
    if not hr:
        return {"linked": False, "why": "The Watch workout has no heart-rate samples."}
    hr.sort(key=lambda x: x[0])
    gaps = [(hr[i + 1][0] - hr[i][0]).total_seconds() for i in range(len(hr) - 1)]
    step = sorted(gaps)[len(gaps) // 2] if gaps else 60.0
    hr_max = strain.max_hr(siris, siris.health.workouts((datetime.now(UTC) - timedelta(days=180)).date().isoformat()))
    start = _aware(session.get("started_at"))
    done = sorted(((s, t) for s in session.get("sets") or [] if (t := _aware(s.get("at")))), key=lambda x: x[1])

    def window(lo: datetime, hi: datetime) -> list[tuple[datetime, float, float]]:
        return [p for p in hr if p[0] + timedelta(seconds=step) > lo and p[0] <= hi]

    sets_out = []
    prev_end = start
    for i, (s, t) in enumerate(done):
        dur = float(s.get("seconds") or 0) or max(20.0, 4.0 * float(s.get("reps") or 0))
        s_start = max(t - timedelta(seconds=dur), prev_end or t - timedelta(seconds=dur))
        s_end = t + timedelta(seconds=20)
        w = window(s_start, s_end) or window(t - timedelta(seconds=step), t + timedelta(seconds=step))
        nxt = done[i + 1][1] - timedelta(seconds=max(20.0, 4.0 * float(done[i + 1][0].get("reps") or 0))) if i + 1 < len(done) else None
        rest = window(s_end, nxt) if nxt and nxt > s_end else []
        peak = max((p[2] for p in w), default=None)
        entry = {"exercise": s.get("name") or s.get("exercise"), "set": s.get("set"), "type": s.get("type", "normal"), "weight": s.get("weight"),
                 "reps": s.get("reps"), "rpe": s.get("rpe"), "at": t.isoformat(), "avg_hr": round(mean(p[1] for p in w)) if w else None,
                 "peak_hr": round(peak) if peak else None, "peak_share": round(peak / hr_max, 2) if peak else None,
                 "recovery_bpm": round(peak - min(p[1] for p in rest)) if peak and rest else None,
                 "rest_s": round((nxt - s_end).total_seconds()) if nxt and nxt > s_end else None}
        sets_out.append(entry)
        prev_end = t
    exercises: dict[str, dict[str, Any]] = {}
    for x in sets_out:
        if x["type"] == "warmup":
            continue
        e = exercises.setdefault(x["exercise"], {"exercise": x["exercise"], "sets": 0, "peaks": [], "avgs": [], "rpes": [], "rec": [], "e1rm": None})
        e["sets"] += 1
        if x["peak_hr"]:
            e["peaks"].append(x["peak_hr"])
        if x["avg_hr"]:
            e["avgs"].append(x["avg_hr"])
        if x["rpe"]:
            e["rpes"].append(float(x["rpe"]))
        if x["recovery_bpm"] is not None:
            e["rec"].append(x["recovery_bpm"])
        if (v := _e1rm(x["weight"], x["reps"])) and (e["e1rm"] is None or v > e["e1rm"]):
            e["e1rm"] = v
    ex_out = []
    for e in exercises.values():
        peak = mean(e["peaks"]) if e["peaks"] else None
        hr_effort = min(10.0, max(0.0, (peak / hr_max - 0.45) / 0.5 * 10)) if peak else None
        rpe = mean(e["rpes"]) if e["rpes"] else None
        effort = round(mean([v for v in (hr_effort, rpe) if v is not None]), 1) if (hr_effort is not None or rpe is not None) else None
        ex_out.append({"exercise": e["exercise"], "sets": e["sets"], "avg_hr": round(mean(e["avgs"])) if e["avgs"] else None,
                       "peak_hr": round(max(e["peaks"])) if e["peaks"] else None, "peak_share": round(peak / hr_max, 2) if peak else None,
                       "effort": effort, "rpe": round(rpe, 1) if rpe else None, "recovery_bpm": round(mean(e["rec"])) if e["rec"] else None,
                       "e1rm": e["e1rm"]})
    in_session = [p for p in hr if start is None or p[0] >= start - timedelta(minutes=2)]
    zones = [0] * 6
    for p in in_session:
        zones[_band(p[1] / hr_max)] += step / 60
    work = [x["avg_hr"] for x in sets_out if x["avg_hr"] and x["type"] != "warmup"]
    kcal = sum(float(k[1]) for k in watch.get("kcal") or [])
    hardest = max((e for e in ex_out if e["effort"] is not None), key=lambda e: e["effort"], default=None)
    slowest = min((e for e in ex_out if e["recovery_bpm"] is not None), key=lambda e: e["recovery_bpm"], default=None)
    parts = []
    if in_session:
        parts.append(f"Heart rate averaged {round(mean(p[1] for p in in_session))} bpm, peaking at {round(max(p[2] for p in in_session))} "
                     f"({round(max(p[2] for p in in_session) / hr_max * 100)}% of your max).")
    if hardest:
        parts.append(f"Hardest: {hardest['exercise']}, effort {hardest['effort']:g}/10 with peaks of {hardest['peak_hr']} bpm.")
    easiest = min((e for e in ex_out if e["effort"] is not None), key=lambda e: e["effort"], default=None)
    if easiest and hardest and easiest is not hardest:
        parts.append(f"Easiest: {easiest['exercise']} ({easiest['effort']:g}/10).")
    if slowest and len(ex_out) > 1:
        parts.append(f"Slowest to recover: {slowest['exercise']} (heart rate dropped {slowest['recovery_bpm']} bpm by the next set).")
    if step >= 50:
        parts.append("The Watch sent heart rate a minute at a time, so each set's figures cover the minutes around it.")
    return {"linked": True, "watch": {"name": watch.get("name"), "start": watch.get("start"), "end": watch.get("end")},
            "max_hr": hr_max, "resolution_s": round(step), "avg_hr": round(mean(p[1] for p in in_session)) if in_session else None,
            "peak_hr": round(max(p[2] for p in in_session)) if in_session else None, "kj": round(kcal * 4.184) if kcal else None,
            "zones_min": [round(z, 1) for z in zones], "work_avg_hr": round(mean(work)) if work else None,
            "exercises": ex_out, "sets": sets_out, "timeline": [[p[0].isoformat(), p[1], p[2]] for p in in_session],
            "text": " ".join(parts)}


def latest_text(siris: "SirisAI") -> str | None:
    kept = siris.state.get(_key("gym_sessions")) or {}
    if not kept:
        return None
    last = max(kept.values(), key=lambda s: str(s.get("started_at")))
    a = analyse(siris, str(last["id"]))
    return f"Last workout ({last.get('title')}): {a['text']}" if a.get("linked") and a.get("text") else None
